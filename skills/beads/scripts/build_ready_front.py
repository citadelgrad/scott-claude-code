"""Deterministic, mutation-free ready-front selection over a captured
Beads snapshot.

Implements AC-T11-001 (ready membership correctness), AC-T11-002
(root/cycle/orientation/same-batch/gate/staleness lanes never dispatch),
and AC-T11-004 (parsing and selection are deterministic and perform no
mutation). This module performs **no I/O** and issues **no** ``bd``
call, directly or indirectly: it consumes only already-captured
JSON-shaped data produced by ``capture_beads_snapshot.py`` (or
synthesized by tests). See
``skills/beads/references/dependencies-and-ready-fronts.md`` for the
normative behavior this module implements.

Algorithm (two phases):

Phase 1 -- global validity gates, checked once across the whole ready
pool, in this fixed precedence order. Any failure refuses the WHOLE
front (``selected == []``), even when other candidates are individually
clean -- this "fail closed" behavior is intentional: a poisoned
dependency graph, stale snapshot, or unresolved gate anywhere in the
pool means the pool as a whole cannot be trusted for dispatch.

  1. cycle check
  2. staleness check
  3. dependency orientation check (only if ``declared_edges`` supplied)
  4. per-candidate poisoning scan (snapshot consistency, gates) --
     first offending candidate in bd-ready order wins
  5. capacity configuration check

Phase 2 -- per-candidate greedy selection (only entered if Phase 1
found no problems). A partial front is a legitimate outcome here: root
issues, non-writing issue types, same-batch unlock violations, and
write-scope conflicts exclude only the offending candidate, not the
whole pool.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

SCHEMA_VERSION = "beads.ready-front.v1"

DEFAULT_NON_WRITING_TYPES = frozenset({"epic", "gate", "convoy"})

_RESOLVED_GATE_STATUSES = frozenset({"resolved", "approved", "closed", "dismissed"})
_ASYNC_GATE_TYPES = frozenset({"async", "ci"})
_HUMAN_GATE_TYPES = frozenset({"human"})

# Whole-front refusal reasons (poison the entire candidate pool).
CYCLE_CHECK_UNKNOWN = "CYCLE_CHECK_UNKNOWN"
DEPENDENCY_CYCLE = "DEPENDENCY_CYCLE"
STALE_READY_FRONT = "STALE_READY_FRONT"
DEPENDENCY_ORIENTATION_UNKNOWN = "DEPENDENCY_ORIENTATION_UNKNOWN"
DEPENDENCY_ORIENTATION_MISMATCH = "DEPENDENCY_ORIENTATION_MISMATCH"
SNAPSHOT_INCONSISTENT = "SNAPSHOT_INCONSISTENT"
UNRESOLVED_HUMAN_GATE = "UNRESOLVED_HUMAN_GATE"
UNRESOLVED_ASYNC_GATE = "UNRESOLVED_ASYNC_GATE"
GATE_UNSUPPORTED = "GATE_UNSUPPORTED"
GATE_READBACK_FAILED = "GATE_READBACK_FAILED"
CAPACITY_UNAVAILABLE = "CAPACITY_UNAVAILABLE"
CAPACITY_EXCEEDED = "CAPACITY_EXCEEDED"

WHOLE_FRONT_REASONS = frozenset(
    {
        CYCLE_CHECK_UNKNOWN,
        DEPENDENCY_CYCLE,
        STALE_READY_FRONT,
        DEPENDENCY_ORIENTATION_UNKNOWN,
        DEPENDENCY_ORIENTATION_MISMATCH,
        SNAPSHOT_INCONSISTENT,
        UNRESOLVED_HUMAN_GATE,
        UNRESOLVED_ASYNC_GATE,
        GATE_UNSUPPORTED,
        GATE_READBACK_FAILED,
        CAPACITY_UNAVAILABLE,
        CAPACITY_EXCEEDED,
    }
)

# Per-candidate exclusion reasons (partial fronts remain possible).
ROOT_ISSUE_EXCLUDED = "ROOT_ISSUE_EXCLUDED"
NON_WRITING_ISSUE_TYPE = "NON_WRITING_ISSUE_TYPE"
SAME_BATCH_UNLOCK_FORBIDDEN = "SAME_BATCH_UNLOCK_FORBIDDEN"
WRITE_SCOPE_CONFLICT = "WRITE_SCOPE_CONFLICT"
WRITE_SCOPE_UNKNOWN = "WRITE_SCOPE_UNKNOWN"
CAPACITY_REACHED = "CAPACITY_REACHED"

PER_CANDIDATE_REASONS = frozenset(
    {
        ROOT_ISSUE_EXCLUDED,
        NON_WRITING_ISSUE_TYPE,
        SAME_BATCH_UNLOCK_FORBIDDEN,
        WRITE_SCOPE_CONFLICT,
        WRITE_SCOPE_UNKNOWN,
        CAPACITY_REACHED,
    }
)


class ReadyFrontError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(code if not message else f"{code}: {message}")
        self.code = code


@dataclass(frozen=True)
class FrontResult:
    schema_version: str
    selected: tuple[str, ...]
    excluded: tuple[Mapping[str, Any], ...]
    refusal: str | None
    diagnostics: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "selected": list(self.selected),
            "excluded": [dict(entry) for entry in self.excluded],
            "refusal": self.refusal,
            "diagnostics": dict(self.diagnostics),
        }


def _age_seconds(captured_at: str, now: str) -> float | None:
    try:
        captured = datetime.strptime(captured_at, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=timezone.utc
        )
        current = datetime.strptime(now, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None
    return (current - captured).total_seconds()


def _observed_edges(ready: Sequence[Mapping[str, Any]]) -> dict[str, list[str]]:
    observed: dict[str, list[str]] = {}
    for record in ready:
        issue_id = record.get("id")
        if issue_id is None:
            continue
        observed[issue_id] = list(record.get("dependency_ids") or [])
    return observed


def _gate_refusal(gate_record: Mapping[str, Any]) -> str | None:
    gate_id = gate_record.get("id")
    status = gate_record.get("status")
    gate_type = gate_record.get("type")
    if not gate_id or not status or not gate_type:
        return GATE_READBACK_FAILED
    resolved = (
        status in _RESOLVED_GATE_STATUSES or gate_record.get("resolved_at") is not None
    )
    if resolved:
        return None
    if gate_type in _HUMAN_GATE_TYPES:
        return UNRESOLVED_HUMAN_GATE
    if gate_type in _ASYNC_GATE_TYPES:
        return UNRESOLVED_ASYNC_GATE
    return GATE_UNSUPPORTED


def _fallback_sorted(ready: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    def key(record: Mapping[str, Any]) -> tuple[int, bytes]:
        priority = record.get("priority")
        priority_value = priority if isinstance(priority, int) else 4
        issue_id = str(record.get("id", ""))
        return (priority_value, issue_id.encode("utf-8"))

    return sorted(ready, key=key)


def _conflict_verdict(
    conflict_matrix: Mapping[str, Mapping[str, Any]],
    issue_id: str,
    selected: Sequence[str],
) -> str | None:
    order = {"safe": 0, "unknown": 1, "conflict": 2}
    worst: str | None = None
    for other in selected:
        key = f"{issue_id}|{other}" if issue_id <= other else f"{other}|{issue_id}"
        entry = conflict_matrix.get(key)
        if entry is None:
            continue
        verdict = entry.get("verdict")
        if verdict is None:
            continue
        if worst is None or order.get(verdict, 0) > order.get(worst, 0):
            worst = verdict
    return worst


def evaluate(
    snapshot: Mapping[str, Any],
    *,
    hard_cap: int | None,
    requested_cap: int | None = None,
    now: str | None = None,
    max_snapshot_age_seconds: int | None = None,
    stale_reasons: Sequence[str] = (),
    declared_edges: Mapping[str, Sequence[str]] | None = None,
    gates: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    consistency: Mapping[str, Mapping[str, Any]] | None = None,
    non_writing_issue_types: frozenset[str] = DEFAULT_NON_WRITING_TYPES,
    conflict_matrix: Mapping[str, Mapping[str, Any]] | None = None,
    live_ready_order: bool = True,
) -> FrontResult:
    """Select a deterministic ready front from ``snapshot``.

    ``snapshot`` is the JSON-shaped dict produced by
    ``capture_beads_snapshot.py`` (must supply ``ready`` and ``cycles``
    at minimum). ``conflict_matrix`` is expected pre-indexed by
    ``detect_write_conflicts.index_by_pair_key`` -- this module never
    imports ``detect_write_conflicts`` itself, to stay a pure function
    over plain data.
    """
    ready = list(snapshot.get("ready") or [])
    root_issue_id = snapshot.get("root_issue_id")

    def refuse(reason: str, **diagnostics: Any) -> FrontResult:
        return FrontResult(SCHEMA_VERSION, (), (), reason, diagnostics)

    # Phase 1.1: cycle check.
    cycles = snapshot.get("cycles")
    if (
        not isinstance(cycles, Mapping)
        or not isinstance(cycles.get("cycles"), list)
        or not isinstance(cycles.get("count"), int)
    ):
        return refuse(CYCLE_CHECK_UNKNOWN, detail="missing or malformed cycle evidence")
    if cycles["count"] != len(cycles["cycles"]):
        return refuse(CYCLE_CHECK_UNKNOWN, detail="cycle count does not match evidence")
    if cycles["cycles"]:
        return refuse(DEPENDENCY_CYCLE, cycles=cycles["cycles"])

    # Phase 1.2: staleness check.
    if stale_reasons:
        return refuse(STALE_READY_FRONT, reasons=list(stale_reasons))
    captured_at = snapshot.get("captured_at")
    if max_snapshot_age_seconds is not None and captured_at and now:
        age = _age_seconds(captured_at, now)
        if age is None or age > max_snapshot_age_seconds:
            return refuse(
                STALE_READY_FRONT,
                detail="snapshot age exceeds max_snapshot_age_seconds",
                age_seconds=age,
            )

    # Phase 1.3: dependency orientation check (only if declared_edges given).
    if declared_edges is not None:
        observed = _observed_edges(ready)
        for issue_id, declared in declared_edges.items():
            if issue_id not in observed:
                return refuse(DEPENDENCY_ORIENTATION_UNKNOWN, issue_id=issue_id)
            if set(declared) != set(observed[issue_id]):
                return refuse(
                    DEPENDENCY_ORIENTATION_MISMATCH,
                    issue_id=issue_id,
                    declared=list(declared),
                    observed=list(observed[issue_id]),
                )

    # Phase 1.4: per-candidate poisoning scan, bd-ready order, first offender wins.
    consistency = consistency or {}
    gates = gates or {}
    for record in ready:
        issue_id = record.get("id")
        check = consistency.get(issue_id)
        if check is not None and check.get("consistent") is False:
            return refuse(
                SNAPSHOT_INCONSISTENT, issue_id=issue_id, detail=check.get("detail")
            )
        for gate_record in gates.get(issue_id, ()):
            reason = _gate_refusal(gate_record)
            if reason is not None:
                return refuse(reason, issue_id=issue_id, gate=dict(gate_record))

    # Phase 1.5: capacity configuration check.
    if hard_cap is None or hard_cap <= 0:
        return refuse(CAPACITY_UNAVAILABLE, hard_cap=hard_cap)
    effective_cap = hard_cap
    if requested_cap is not None:
        if requested_cap > hard_cap:
            return refuse(
                CAPACITY_EXCEEDED, requested_cap=requested_cap, hard_cap=hard_cap
            )
        effective_cap = requested_cap

    # Phase 2: per-candidate greedy selection.
    ordered = ready if live_ready_order else _fallback_sorted(ready)

    selected: list[str] = []
    excluded: list[dict[str, Any]] = []
    selected_status: dict[str, str] = {}

    for record in ordered:
        # Coerce to str up front (matches ``_fallback_sorted``'s own
        # ``key()`` idiom): every well-formed ready record's "id" is
        # already a string, so this is a no-op for real input; it only
        # changes behavior for a malformed record with a missing/non-str
        # "id", turning what would otherwise be a raw ``None`` leaking
        # into downstream dict payloads/comparisons into a stable typed
        # value instead.
        issue_id = str(record.get("id", ""))
        if issue_id == root_issue_id:
            excluded.append({"id": issue_id, "reason": ROOT_ISSUE_EXCLUDED})
            continue

        issue_type = record.get("issue_type")
        if issue_type in non_writing_issue_types:
            excluded.append({"id": issue_id, "reason": NON_WRITING_ISSUE_TYPE})
            continue

        deps = record.get("dependency_ids") or []
        blocked_by_batch = any(
            dep_id in selected and selected_status.get(dep_id) != "closed"
            for dep_id in deps
        )
        if blocked_by_batch:
            excluded.append({"id": issue_id, "reason": SAME_BATCH_UNLOCK_FORBIDDEN})
            continue

        if conflict_matrix is not None:
            verdict = _conflict_verdict(conflict_matrix, issue_id, selected)
            if verdict == "conflict":
                excluded.append({"id": issue_id, "reason": WRITE_SCOPE_CONFLICT})
                continue
            if verdict == "unknown":
                excluded.append({"id": issue_id, "reason": WRITE_SCOPE_UNKNOWN})
                continue

        if len(selected) >= effective_cap:
            excluded.append({"id": issue_id, "reason": CAPACITY_REACHED})
            continue

        selected.append(issue_id)
        selected_status[issue_id] = record.get("status", "")

    return FrontResult(SCHEMA_VERSION, tuple(selected), tuple(excluded), None, {})
