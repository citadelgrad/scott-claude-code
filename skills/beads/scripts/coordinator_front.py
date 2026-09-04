#!/usr/bin/env python3
"""Ready-front coordinator composition: snapshot -> conflict matrix -> front.

Composes ``capture_beads_snapshot.capture_snapshot``,
``detect_write_conflicts.build_conflict_matrix`` /
``detect_write_conflicts.index_by_pair_key``, and
``build_ready_front.evaluate`` into the single pipeline the Task-8
coordinator dispatcher composes (spec section C9). This module performs no
Beads, Hermes, Dolt-remote, push, or secret-bearing action itself: the
only ``bd`` access anywhere in the pipeline is ``capture_beads_snapshot``'s
five read-only ``safe_bd.run_profile`` calls. This module adds no
additional selection policy of its own -- every eligibility and conflict
rule stays owned by ``build_ready_front`` and ``detect_write_conflicts``;
this module only reshapes their inputs.

Implements AC-T11-001..004 at the composition layer:

* ``build_candidates`` turns each ready-front record's declared
  ``owned_paths`` into the ``{"id", "scopes"}`` shape
  ``detect_write_conflicts.build_conflict_matrix`` expects.
* ``group_gates_by_target`` groups the snapshot's captured gate records
  by the issue they block. ``safe_bd``'s gate record schema
  (``_GATE_FIELDS``) has no ``issue_id`` field -- the correct grouping
  key is ``target``.
* ``build_front_from_snapshot`` is a pure function over an
  already-captured snapshot plus ``repository_root``: it issues no
  ``bd`` call and mutates nothing, so it can be replayed byte-for-byte
  over a persisted ``snapshot.json`` (AC-T11-004).
* ``capture_and_build_front`` is the end-to-end convenience wrapper that
  captures a fresh snapshot and evaluates it in one call, returning both
  the snapshot and the front so a caller can persist the exact evidence
  the front was computed from.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

import build_ready_front  # noqa: E402
import capture_beads_snapshot  # noqa: E402
import detect_write_conflicts  # noqa: E402

SCHEMA_VERSION = build_ready_front.SCHEMA_VERSION


class CoordinatorFrontError(Exception):
    """Typed, fail-closed composition failure (``.code``).

    Raised only for shapes this composition layer cannot safely reshape
    for its callees -- every eligibility, conflict, or staleness
    judgement itself stays inside ``build_ready_front`` and
    ``detect_write_conflicts``, which are never bypassed here.
    """

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(code if not message else f"{code}: {message}")
        self.code = code


def _gate_target(record: Mapping[str, Any]) -> str | None:
    target = record.get("target")
    if isinstance(target, str) and target:
        return target
    return None


def group_gates_by_target(
    gates: Sequence[Mapping[str, Any]],
) -> dict[str, list[Mapping[str, Any]]]:
    """Group captured gate records by the issue id they block.

    A gate record with a missing or non-string ``target`` cannot be
    attributed to any issue and is dropped defensively rather than
    raising -- mirroring ``capture_beads_snapshot._owned_paths``'s
    precedent of dropping malformed per-issue metadata rather than
    aborting the whole snapshot. This is safe, not permissive: an
    unattributable gate simply cannot block a candidate it was never
    linked to, and every gate record that *does* carry a valid
    ``target`` is still passed through unmodified, so
    ``build_ready_front._gate_refusal`` sees any other malformation
    (missing ``id``/``status``/``type``) itself and fails that
    candidate's whole front closed via ``GATE_READBACK_FAILED``.
    """
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for record in gates:
        target = _gate_target(record)
        if target is None:
            continue
        grouped.setdefault(target, []).append(record)
    return grouped


def build_candidates(ready: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Project ready-front records into ``detect_write_conflicts`` candidates.

    Raises ``CoordinatorFrontError("READY_RECORD_MISSING_ID")`` for any
    record without a non-empty string ``id`` rather than letting a
    malformed snapshot fail with an opaque ``KeyError`` deep inside
    conflict-matrix construction.
    """
    candidates: list[dict[str, Any]] = []
    for record in ready:
        issue_id = record.get("id")
        if not isinstance(issue_id, str) or not issue_id:
            raise CoordinatorFrontError("READY_RECORD_MISSING_ID", repr(issue_id))
        candidates.append(
            {"id": issue_id, "scopes": list(record.get("owned_paths") or [])}
        )
    return candidates


def build_front_from_snapshot(
    snapshot: Mapping[str, Any],
    *,
    repository_root: str,
    hard_cap: int | None,
    requested_cap: int | None = None,
    now: str | None = None,
    max_snapshot_age_seconds: int | None = None,
    stale_reasons: Sequence[str] = (),
    declared_edges: Mapping[str, Sequence[str]] | None = None,
    consistency: Mapping[str, Mapping[str, Any]] | None = None,
    non_writing_issue_types: frozenset[
        str
    ] = build_ready_front.DEFAULT_NON_WRITING_TYPES,
    live_ready_order: bool = True,
) -> build_ready_front.FrontResult:
    """Evaluate one already-captured snapshot into a ready front.

    Pure function over ``snapshot`` plus ``repository_root``: builds the
    write-scope conflict matrix from each ready candidate's declared
    ``owned_paths`` (AC-T11-003) and groups the snapshot's captured gate
    records by ``target`` (AC-T11-002), then delegates selection
    unmodified to ``build_ready_front.evaluate`` (AC-T11-001/002).
    Issues no ``bd`` call and mutates nothing (AC-T11-004): this
    function can be called any number of times over the same
    ``snapshot`` and always returns the identical result.
    """
    ready = list(snapshot.get("ready") or [])
    candidates = build_candidates(ready)
    matrix = detect_write_conflicts.build_conflict_matrix(candidates, repository_root)
    indexed = detect_write_conflicts.index_by_pair_key(matrix)
    gates = group_gates_by_target(list(snapshot.get("gates") or []))
    return build_ready_front.evaluate(
        snapshot,
        hard_cap=hard_cap,
        requested_cap=requested_cap,
        now=now,
        max_snapshot_age_seconds=max_snapshot_age_seconds,
        stale_reasons=stale_reasons,
        declared_edges=declared_edges,
        gates=gates,
        consistency=consistency,
        non_writing_issue_types=non_writing_issue_types,
        conflict_matrix=indexed,
        live_ready_order=live_ready_order,
    )


def capture_and_build_front(
    *,
    repository_root: Path,
    root_issue_id: str | None = None,
    limit: int = 100,
    hard_cap: int | None,
    requested_cap: int | None = None,
    now: str | None = None,
    max_snapshot_age_seconds: int | None = None,
    stale_reasons: Sequence[str] = (),
    declared_edges: Mapping[str, Sequence[str]] | None = None,
    consistency: Mapping[str, Mapping[str, Any]] | None = None,
    non_writing_issue_types: frozenset[
        str
    ] = build_ready_front.DEFAULT_NON_WRITING_TYPES,
    live_ready_order: bool = True,
    clock: Callable[[], datetime] | None = None,
) -> tuple[dict[str, Any], build_ready_front.FrontResult]:
    """Capture one fresh Beads snapshot and evaluate it into a ready front.

    Wraps ``capture_beads_snapshot.capture_snapshot`` (the sole ``bd``
    entry point anywhere in this pipeline) and
    ``build_front_from_snapshot``. Returns ``(snapshot, front)`` so a
    caller can persist the exact evidence the front was computed from
    (e.g. via ``capture_beads_snapshot.persist_snapshot``).
    ``capture_beads_snapshot.SnapshotCaptureError`` propagates unchanged
    on capture failure: this function never fabricates a front over
    unavailable or inconsistent evidence.
    """
    capture_kwargs: dict[str, Any] = {}
    if clock is not None:
        capture_kwargs["clock"] = clock
    snapshot = capture_beads_snapshot.capture_snapshot(
        repository_root=repository_root,
        root_issue_id=root_issue_id,
        limit=limit,
        **capture_kwargs,
    )
    front = build_front_from_snapshot(
        snapshot,
        repository_root=str(repository_root),
        hard_cap=hard_cap,
        requested_cap=requested_cap,
        now=now,
        max_snapshot_age_seconds=max_snapshot_age_seconds,
        stale_reasons=stale_reasons,
        declared_edges=declared_edges,
        consistency=consistency,
        non_writing_issue_types=non_writing_issue_types,
        live_ready_order=live_ready_order,
    )
    return snapshot, front
