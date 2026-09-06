"""Gate for effects the runtime is forbidden to perform on its own.

``direct_operation.py`` covers tracker mutations the coordinator is trusted to
apply itself, verified afterwards by an independent readback.  Some effects
never get that trust — pushing a ref, spending money, touching a secret,
deploying to production, or anything else with consequences outside this
run's own owned state.  For those, this module never dispatches the effect;
it only journals the intent (:func:`prepare_protected_action`), and later
verifies that a human/current-harness actor did the thing, by demanding a
schema-valid receipt *and* an independently observed readback that agrees
with it (:func:`resolve_protected_action`).  Disagreement, silence, or an
unreadable target never becomes success — see ``classify_observation``'s
four-way outcome below.

A second, narrower gate lives here too: parent-attested approval grants
(``approval-record-v1``) can authorize exactly one durable design decision,
never a protected effect, no matter what the grant's own label claims.
:func:`consume_approval_grant` enforces that by allow-listing a single
grantable class rather than denying a fixed list of dangerous ones, so an
effect class nobody has named yet still fails closed.

This module never calls ``safe_bd.run_profile`` or ``direct_operation``'s
mutation surface — a protected effect has no tracker-mutation profile to
dispatch through in the first place.  Its only subprocess capability is the
declared read-only recovery probe, run through ``safe_output.run_command``
(profile ``"recovery_probe"``), and only from :func:`resolve_protected_action`
after every binding check has already passed.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).parent))

import coordinator_state as state
import direct_operation
import safe_output
import schema_runtime

# ---------------------------------------------------------------------------
# Reused surface.  ``direct_operation`` owns the four-way classification and
# the journal-event builders; protected effects share the same vocabulary
# rather than inventing a parallel one, per its own module docstring naming
# this module as a consumer.
# ---------------------------------------------------------------------------
APPLIED = direct_operation.APPLIED
NOT_APPLIED = direct_operation.NOT_APPLIED
CONFLICT = direct_operation.CONFLICT
UNKNOWN = direct_operation.UNKNOWN
CLASSIFICATION_STATUS = direct_operation.CLASSIFICATION_STATUS

# ---------------------------------------------------------------------------
# Own vocabulary.
# ---------------------------------------------------------------------------
HUMAN_ACTION_REQUIRED = "human_action_required"

# A grant may authorize exactly this one class of decision.  Enforced as an
# allow-list of one rather than a blocklist of six, so "or other protected
# effects" (AC-T12-005) is covered by construction, not by remembering to
# extend a list every time someone invents a new dangerous category.
ALLOWED_GRANT_CLASS = "design_decision"
NEVER_GRANTABLE_CLASSES = frozenset(
    {"production", "spend", "destructive", "secret", "push", "dolt_remote"}
)

APPROVAL_CONSUME_EFFECT_TYPE = "APPROVAL_CONSUME"

# Kept distinct from direct_operation's own OPERATIONS_DIRNAME ("_operations")
# so the two effect layers can never collide on a filename within one run.
PROTECTED_DIRNAME = "_protected"

MAX_INTENT_BYTES = direct_operation.MAX_INTENT_BYTES
MAX_EVIDENCE_BYTES = direct_operation.MAX_EVIDENCE_BYTES


class ProtectedActionError(ValueError):
    """Typed fail-closed refusal (``.code``, ``.status``).

    Mirrors :class:`direct_operation.DirectOperationError`'s shape without
    reusing it: the two error types describe refusals in distinct effect
    layers, and callers should be able to tell which layer refused.
    """

    def __init__(self, code: str, *, status: str = CONFLICT) -> None:
        self.code = code
        self.status = status
        super().__init__(code)


def _require_valid(schema_name: str, value: Mapping[str, Any]) -> None:
    try:
        schema_runtime.require_valid(schema_name, dict(value))
    except schema_runtime.ValidationFailure as exc:
        raise ProtectedActionError(f"SCHEMA_INVALID:{schema_name}") from exc


def _protected_directory(context: direct_operation.RunContext) -> Path:
    """Return (creating if needed) this run's protected-action directory."""
    target = context.run_directory / PROTECTED_DIRNAME
    state.ensure_owner_directory(target, root=context.run_directory)
    return target


def _sanitize_field(value: str, sensitive: safe_output.SensitiveSet) -> str:
    """Run one receipt string through the shared redaction gate.

    Nothing a harness or human hands back to us becomes durable — journalled,
    checkpointed, or echoed into evidence — until it has passed this.  A
    field that cannot be made safe fails closed rather than being dropped
    silently, since a silently dropped field could hide a mismatch.
    """
    cleaned = safe_output.sanitize_value(
        value, sensitive=sensitive, max_utf8_bytes=MAX_EVIDENCE_BYTES
    )
    if cleaned.status == "REDACTION_FAILED" or cleaned.value is None:
        raise ProtectedActionError(f"RECEIPT_FIELD_UNSAFE:{cleaned.error_code}")
    return cleaned.value


def _sanitize_receipt(
    receipt: Mapping[str, Any], *, sensitive: safe_output.SensitiveSet
) -> dict[str, Any]:
    sanitized = dict(receipt)
    for key in ("observed_identity", "harness", "provider", "version"):
        value = receipt.get(key)
        if isinstance(value, str):
            sanitized[key] = _sanitize_field(value, sensitive)
    evidence = dict(receipt.get("evidence") or {})
    summary = evidence.get("summary")
    if isinstance(summary, str):
        evidence["summary"] = _sanitize_field(summary, sensitive)
    sanitized["evidence"] = evidence
    return sanitized


def _pending_action(
    *,
    action: str,
    operation_id: str,
    run_id: str | None,
    issue_id: str | None,
    ownership_epoch: int | None,
    target_sha256: str,
    precondition_sha256: str,
    authority_class: str,
    payload: Mapping[str, Any],
    expires_at: str | None,
    required_receipt_variant: str,
) -> dict[str, Any]:
    payload = dict(payload)
    payload_sha256 = state.sha256_bytes(state.canonical_payload_bytes(payload))
    body: dict[str, Any] = {
        "schema_version": "beads.pending-action.v1",
        "action": action,
        "operation_id": operation_id,
        "run_id": run_id,
        "issue_id": issue_id,
        "ownership_epoch": ownership_epoch,
        "target_sha256": target_sha256,
        "precondition_sha256": precondition_sha256,
        "payload_sha256": payload_sha256,
        "authority_class": authority_class,
        "payload": payload,
        "expires_at": expires_at,
        "required_receipt_variant": required_receipt_variant,
    }
    # action_id hashes the body it is itself excluded from: computed here,
    # before the key exists, then attached.
    body["action_id"] = state.sha256_bytes(state.canonical_payload_bytes(body))
    _require_valid("pending-action-v1.schema.json", body)
    return body


def consume_operation_id(
    *, approval_id: str, authorized_operation_id: str, ownership_epoch: int | None
) -> str:
    """SHA-256 over the canonical 3-field consume payload (spec 7.8.1).

    Distinct from :func:`coordinator_state.semantic_operation_id`, whose
    5-key identity shape does not fit an approval consume.
    """
    payload = {
        "approval_id": approval_id,
        "authorized_operation_id": authorized_operation_id,
        "ownership_epoch": ownership_epoch,
    }
    return state.sha256_bytes(state.canonical_payload_bytes(payload))


# ---------------------------------------------------------------------------
# AC-T12-004: protected harness effects.
# ---------------------------------------------------------------------------


def prepare_protected_action(
    context: direct_operation.RunContext,
    *,
    effect_type: str,
    target_identity: str,
    target_sha256: str,
    precondition_sha256: str,
    summary: str,
    probe_type: str,
    probe_argv: Sequence[str],
    issue_id: str | None,
    ownership_epoch: int | None,
    authority_class: str = direct_operation.AUTHORITY_CLASS,
    parameters: Sequence[str] = (),
    artifact_path: str | None = None,
    expires_at: str | None = None,
    timeout_seconds: int = direct_operation.PROBE_TIMEOUT_SECONDS,
    required_authority: str = direct_operation.PROBE_AUTHORITY,
    timestamp: str | None = None,
) -> dict[str, Any]:
    """Journal a PREPARED protected-effect record; never perform the effect.

    Returns ``status: HUMAN_ACTION_REQUIRED`` unconditionally — that is the
    whole point of this function: it hands the caller a ``pending-action-v1``
    document describing exactly what a human/current-harness actor must do,
    and it has no code path that dispatches anything itself (no ``runner``
    parameter exists on this function at all).
    """
    ts = timestamp or direct_operation.utc_now()

    immutable_input = {
        "effect_type": effect_type,
        "target_identity": target_identity,
        "target_sha256": target_sha256,
        "precondition_sha256": precondition_sha256,
        "summary": summary,
        "parameters": list(parameters),
        "artifact_path": artifact_path,
    }
    input_raw = state.canonical_bytes(immutable_input)
    input_sha256 = state.sha256_bytes(input_raw)
    operation_id = state.semantic_operation_id(
        {
            "schema": "beads.protected-action.v1",
            "effect_type": effect_type,
            "target_identity": target_identity,
            "immutable_input_sha256": input_sha256,
            "ownership_epoch": ownership_epoch,
        }
    )

    directory = _protected_directory(context)
    input_path = directory / f"{operation_id}.intent.json"
    state.atomic_write(
        input_path, input_raw, root=context.run_directory, max_bytes=MAX_INTENT_BYTES
    )

    probe = {
        "schema_version": "beads.recovery-probe.v1",
        "kind": "request",
        "probe_type": probe_type,
        "target_identity": target_identity,
        "expected_before_sha256": precondition_sha256,
        "intended_after_identity": target_identity,
        "intended_after_sha256": target_sha256,
        "descriptor": list(probe_argv),
        "timeout_seconds": timeout_seconds,
        "required_authority": required_authority,
    }
    _require_valid("recovery-probe-v1.schema.json", probe)

    prepared = direct_operation.prepared_event(
        run_id=context.run_id,
        operation_id=operation_id,
        effect_type=effect_type,
        input_path=input_path,
        input_sha256=input_sha256,
        expected_pre_state_sha256=precondition_sha256,
        probe=probe,
        timestamp=ts,
        issue_id=issue_id,
        ownership_epoch=ownership_epoch,
        authority_class=authority_class,
    )
    try:
        context.journal.append(prepared, hook=context.crash_hook)
    except state.StateError as exc:
        raise ProtectedActionError(exc.code, status=exc.status) from exc

    pending_action = _pending_action(
        action="protected_harness_effect",
        operation_id=operation_id,
        run_id=context.run_id,
        issue_id=issue_id,
        ownership_epoch=ownership_epoch,
        target_sha256=target_sha256,
        precondition_sha256=precondition_sha256,
        authority_class=authority_class,
        payload={
            "summary": summary,
            "artifact_path": artifact_path,
            "parameters": list(parameters),
        },
        expires_at=expires_at,
        required_receipt_variant="protected_harness_effect",
    )

    return {
        "operation_id": operation_id,
        "status": HUMAN_ACTION_REQUIRED,
        "prepared": prepared,
        "pending_action": pending_action,
    }


def resolve_protected_action(
    context: direct_operation.RunContext,
    *,
    prepared: Mapping[str, Any],
    pending_action: Mapping[str, Any],
    receipt: Mapping[str, Any],
    runner: Callable[..., tuple[Any, Any]] = safe_output.run_command,
    sensitive: safe_output.SensitiveSet = direct_operation.NO_SENSITIVE,
    probe_cwd: Path | None = None,
    timestamp: str | None = None,
) -> dict[str, Any]:
    """Verify a harness receipt against an independent readback, and resolve.

    Never trusts the receipt alone: every binding field (``operation_id``,
    ``target_sha256``, ``action_sha256``) must match the prepared action
    *before* the read-only recovery probe declared at prepare time is run —
    a mismatch refuses without ever dispatching.  The probe result, not the
    receipt's own claimed outcome, decides APPLIED / NOT_APPLIED / CONFLICT /
    UNKNOWN.  Already-resolved operations refuse outright rather than
    re-dispatching the probe, so a receipt is not reusable to resolve the
    same action twice.
    """
    ts = timestamp or direct_operation.utc_now()
    operation_id = prepared["operation_id"]

    already_resolved = [
        record
        for record in context.journal.read().records
        if record.get("phase") == "RESOLUTION"
        and record.get("operation_id") == operation_id
    ]
    if already_resolved:
        raise ProtectedActionError("OPERATION_ALREADY_RESOLVED", status=CONFLICT)

    _require_valid("harness-receipt-v1.schema.json", receipt)
    if receipt.get("receipt_variant") != pending_action.get("required_receipt_variant"):
        raise ProtectedActionError("RECEIPT_VARIANT_MISMATCH")
    if (
        receipt.get("operation_id") != operation_id
        or pending_action.get("operation_id") != operation_id
    ):
        raise ProtectedActionError("RECEIPT_OPERATION_MISMATCH")
    if receipt.get("target_sha256") != pending_action.get("target_sha256"):
        raise ProtectedActionError("RECEIPT_TARGET_MISMATCH")
    if receipt.get("action_sha256") != pending_action.get("action_id"):
        raise ProtectedActionError("RECEIPT_ACTION_MISMATCH")

    receipt = _sanitize_receipt(receipt, sensitive=sensitive)

    # The probe actually run is the one committed at prepare time (its
    # descriptor), never one supplied fresh at resolve time — otherwise a
    # resolve call could read back something other than what was promised.
    probe_request = prepared["recovery_probe"]
    argv = tuple(probe_request["descriptor"])
    spec = safe_output.CommandSpec(
        profile="recovery_probe",
        argv=argv,
        cwd=probe_cwd or context.repository_root,
        stdin_bytes=None,
        timeout_seconds=int(
            probe_request.get("timeout_seconds")
            or direct_operation.PROBE_TIMEOUT_SECONDS
        ),
        stdout_limit_bytes=MAX_EVIDENCE_BYTES,
        stderr_limit_bytes=MAX_EVIDENCE_BYTES,
        total_limit_bytes=MAX_EVIDENCE_BYTES,
        output_codec="utf8_text",
        stdout_log=None,
        stderr_log=None,
    )

    try:
        result, observed_text = runner(
            spec, sensitive=sensitive, callback=lambda out, err: out.strip()
        )
    except safe_output.SafeOutputError:
        result, observed_text = None, None

    observed_identity: str | None = None
    if isinstance(observed_text, str) and observed_text:
        observed_identity = _sanitize_field(observed_text, sensitive)

    dispatched_ok = bool(
        result is not None
        and getattr(result, "exit_code", None) == 0
        and getattr(result, "error_code", None) is None
    )
    observed: dict[str, Any] | None = None
    observed_sha256: str | None = None
    if dispatched_ok and observed_identity:
        observed_sha256 = state.sha256_bytes(observed_identity.encode("utf-8"))
        observed = {"sha256": observed_sha256}

    intended = {"sha256": pending_action["target_sha256"]}
    prestate = {"sha256": pending_action["precondition_sha256"]}
    classification = direct_operation.classify_observation(
        observed, intended=intended, prestate=prestate
    )
    status = CLASSIFICATION_STATUS[classification]

    evidence_payload = {
        "receipt": receipt,
        "observed_identity": observed_identity,
        "observed_sha256": observed_sha256,
        "probe_status": getattr(result, "status", None) if result is not None else None,
    }
    evidence_dir = _protected_directory(context)
    evidence_path = evidence_dir / f"{operation_id}.resolve.evidence.json"
    evidence_raw = state.canonical_bytes(evidence_payload)
    state.atomic_write(
        evidence_path,
        evidence_raw,
        root=context.run_directory,
        max_bytes=MAX_EVIDENCE_BYTES,
    )
    evidence_sha256 = state.sha256_bytes(evidence_raw)

    probe_outcome = direct_operation.probe_result(
        probe_request,
        classification=classification,
        observed_identity=observed_identity,
        observed_sha256=observed_sha256,
        observed_state=(
            (getattr(result, "status", None) if result is not None else None)
            or "no_result"
        ),
        evidence_path=evidence_path,
        evidence_sha256=evidence_sha256,
        observed_at=ts,
    )
    _require_valid("recovery-probe-v1.schema.json", probe_outcome)

    resolution = direct_operation.resolution_event(
        prepared,
        status=status,
        observed_post_state_sha256=observed_sha256,
        readback_evidence_path=evidence_path,
        readback_evidence_sha256=evidence_sha256,
        timestamp=ts,
        error_code=None if status == APPLIED else f"PROTECTED_ACTION_{status}",
        template_id="protected_action_unresolved",
        field_path="/protected_action",
    )
    try:
        context.journal.append(resolution, hook=context.crash_hook)
    except state.StateError as exc:
        raise ProtectedActionError(exc.code, status=exc.status) from exc

    return {
        "operation_id": operation_id,
        "status": status,
        "classification": classification,
        "resolution": resolution,
        "probe_result": probe_outcome,
        "receipt": receipt,
    }


# ---------------------------------------------------------------------------
# AC-T12-005: parent-attested approval grants.
# ---------------------------------------------------------------------------


def consume_approval_grant(
    context: direct_operation.RunContext,
    grant: Mapping[str, Any],
    *,
    protected_class: str,
    authorized_operation_id: str,
    target_sha256: str,
    precondition_sha256: str,
    ownership_epoch: int | None,
    responder_identity: str,
    issue_id: str | None = None,
    revoked: bool = False,
    timestamp: str | None = None,
) -> dict[str, Any]:
    """Validate and consume a one-shot grant immediately before its sole use.

    Refuses categorically (``HUMAN_ACTION_REQUIRED``) unless ``protected_class``
    is exactly :data:`ALLOWED_GRANT_CLASS` — a grant can never authorize a
    production, spend, destructive, secret, push, Dolt-remote, or any other
    effect this module doesn't recognise as a durable design decision.  Every
    other field on the grant (provenance, the one authorized operation,
    target, precondition, epoch, one-shot flag, responder, expiry, and
    revocation) is rechecked here — against the grant's own frozen record,
    not against the caller's say-so — because ``grant.json`` is immutable and
    only these checks catch drift since it was issued.

    Consuming the exact same grant for the exact same operation twice is an
    idempotent no-op (the journal's own binding-field replay semantics make
    it so): this is the crash-safe retry the spec calls for, not a second
    real use.  A second attempt naming a *different* ``authorized_operation_id``
    is refused outright by the operation-mismatch check above, before it ever
    reaches the journal — which is what actually keeps a one-shot grant from
    authorizing more than the one operation it names.
    """
    _require_valid("approval-record-v1.schema.json", grant)
    ts = timestamp or direct_operation.utc_now()

    if (
        protected_class in NEVER_GRANTABLE_CLASSES
        or protected_class != ALLOWED_GRANT_CLASS
    ):
        raise ProtectedActionError(
            "GRANT_CLASS_NOT_AUTHORIZABLE", status=HUMAN_ACTION_REQUIRED
        )
    if grant.get("provenance_class") != "parent_attested":
        raise ProtectedActionError(
            "GRANT_PROVENANCE_INVALID", status=HUMAN_ACTION_REQUIRED
        )
    if grant.get("authorized_operation_id") != authorized_operation_id:
        raise ProtectedActionError(
            "GRANT_OPERATION_MISMATCH", status=HUMAN_ACTION_REQUIRED
        )
    if grant.get("target_sha256") != target_sha256:
        raise ProtectedActionError("GRANT_TARGET_CHANGED", status=HUMAN_ACTION_REQUIRED)
    if grant.get("precondition_sha256") != precondition_sha256:
        raise ProtectedActionError(
            "GRANT_PRECONDITION_CHANGED", status=HUMAN_ACTION_REQUIRED
        )
    if grant.get("ownership_epoch") != ownership_epoch:
        raise ProtectedActionError("GRANT_EPOCH_CHANGED", status=HUMAN_ACTION_REQUIRED)
    if grant.get("one_shot") is not True:
        raise ProtectedActionError("GRANT_NOT_ONE_SHOT", status=HUMAN_ACTION_REQUIRED)
    if responder_identity != grant.get("approver_identity"):
        raise ProtectedActionError(
            "GRANT_RESPONDER_UNAUTHORIZED", status=HUMAN_ACTION_REQUIRED
        )
    if revoked:
        raise ProtectedActionError("GRANT_REVOKED", status=HUMAN_ACTION_REQUIRED)
    if grant.get("expiry_policy") == "expires_at":
        expires_at = grant.get("expires_at")
        if not expires_at or ts >= expires_at:
            raise ProtectedActionError("GRANT_EXPIRED", status=HUMAN_ACTION_REQUIRED)

    approval_id = grant["approval_id"]
    consume_id = consume_operation_id(
        approval_id=approval_id,
        authorized_operation_id=authorized_operation_id,
        ownership_epoch=ownership_epoch,
    )
    immutable_input = {
        "approval_id": approval_id,
        "authorized_operation_id": authorized_operation_id,
        "ownership_epoch": ownership_epoch,
        "consume_operation_id": consume_id,
    }
    input_raw = state.canonical_bytes(immutable_input)
    input_sha256 = state.sha256_bytes(input_raw)
    journal_operation_id = state.semantic_operation_id(
        {
            "schema": "beads.protected-action-consume.v1",
            "effect_type": APPROVAL_CONSUME_EFFECT_TYPE,
            "target_identity": approval_id,
            "immutable_input_sha256": input_sha256,
            "ownership_epoch": ownership_epoch,
        }
    )

    directory = _protected_directory(context)
    input_path = directory / f"{journal_operation_id}.consume.json"
    state.atomic_write(
        input_path, input_raw, root=context.run_directory, max_bytes=MAX_INTENT_BYTES
    )

    probe = {
        "schema_version": "beads.recovery-probe.v1",
        "kind": "request",
        "probe_type": "approval_record",
        "target_identity": approval_id,
        "expected_before_sha256": precondition_sha256,
        "intended_after_identity": approval_id,
        "intended_after_sha256": consume_id,
        "descriptor": [approval_id, authorized_operation_id],
        "timeout_seconds": direct_operation.PROBE_TIMEOUT_SECONDS,
        "required_authority": "approval_consume",
    }
    _require_valid("recovery-probe-v1.schema.json", probe)

    prepared = direct_operation.prepared_event(
        run_id=context.run_id,
        operation_id=journal_operation_id,
        effect_type=APPROVAL_CONSUME_EFFECT_TYPE,
        input_path=input_path,
        input_sha256=input_sha256,
        expected_pre_state_sha256=precondition_sha256,
        probe=probe,
        timestamp=ts,
        issue_id=issue_id,
        ownership_epoch=ownership_epoch,
        authority_class="parent_attested",
    )
    try:
        context.journal.append(prepared, hook=context.crash_hook)
    except state.StateError as exc:
        raise ProtectedActionError(exc.code, status=exc.status) from exc

    # Consuming the grant is itself the effect: it is pure journal
    # bookkeeping with no external target to read back, so it resolves
    # APPLIED immediately rather than going through the probe/classify path
    # that external protected effects require.
    resolution = direct_operation.resolution_event(
        prepared,
        status=APPLIED,
        observed_post_state_sha256=consume_id,
        readback_evidence_path=input_path,
        readback_evidence_sha256=input_sha256,
        timestamp=ts,
        template_id="protected_action_grant_consumed",
        field_path="/protected_action/grant",
    )
    try:
        context.journal.append(resolution, hook=context.crash_hook)
    except state.StateError as exc:
        raise ProtectedActionError(exc.code, status=exc.status) from exc

    return {
        "consume_operation_id": consume_id,
        "operation_id": journal_operation_id,
        "prepared": prepared,
        "resolution": resolution,
    }
