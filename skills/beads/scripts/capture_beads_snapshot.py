"""Bulk safe Beads snapshot capture for the ready-front coordinator.

Issues exactly five read-only ``bd`` calls through ``safe_bd.run_profile``
(``ready_list``, ``blocked_list``, ``dependency_cycles``, ``gate_list``,
``human_list``) and projects the results into the plain-data shape that
``build_ready_front.evaluate`` and ``coordinator_front.py`` expect. This
module never mutates Beads state and never accesses Dolt directly -- all
access is through ``safe_bd``, which is itself the only sanctioned ``bd``
entry point (implements the capture half of AC-T11-001..004).
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parent))

import coordinator_state as state  # noqa: E402
import safe_bd  # noqa: E402

SCHEMA_VERSION = "beads.issue-snapshot.v1"
MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024

_READY_SORT = "priority"
_DEFAULT_READY_LIMIT = 100

_CALL_PROFILES = (
    "ready_list",
    "blocked_list",
    "dependency_cycles",
    "gate_list",
    "human_list",
)


class SnapshotCaptureError(Exception):
    """Typed, non-mutating snapshot-capture failure.

    ``code`` is one of ``SNAPSHOT_CALL_FAILED``, ``SNAPSHOT_WORKSPACE_MISMATCH``,
    ``SNAPSHOT_CLI_VERSION_MISMATCH``, ``SNAPSHOT_MALFORMED``, or
    ``SNAPSHOT_TOO_LARGE``. Never fabricates a snapshot on failure -- callers
    get an exception, never a partial/best-effort result.
    """

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(code if not message else f"{code}: {message}")
        self.code = code


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)


def _dependency_ids(record: Mapping[str, Any]) -> list[str]:
    """Derive this issue's prerequisite ids from its raw ``dependencies``.

    ``safe_bd``'s validator accepts two shapes for a ``dependencies`` entry:
    a dependency-record (``{"issue_id", "depends_on_id", "type"}``) or a
    nested full issue record (``{"id", ...}``). Both are handled; unions of
    entries are de-duplicated and returned in a stable sorted order so the
    result is deterministic regardless of ``bd``'s own entry ordering.
    """
    ids: set[str] = set()
    for entry in record.get("dependencies") or []:
        if not isinstance(entry, Mapping):
            continue
        candidate = entry.get("depends_on_id")
        if not isinstance(candidate, str) or not candidate:
            candidate = entry.get("id")
        if isinstance(candidate, str) and candidate:
            ids.add(candidate)
    return sorted(ids)


def _owned_paths(record: Mapping[str, Any]) -> list[str]:
    """Derive this issue's declared write scope from ``metadata.owned_paths``.

    ``metadata.owned_paths`` is the same field Beads issues in this repo
    already carry (e.g. this very task's own issue metadata declares its
    five owned files this way). Non-string or empty entries are dropped
    defensively rather than raising, since a malformed scope entry must
    fail closed as ``unknown`` in ``detect_write_conflicts`` rather than
    aborting the whole snapshot capture.
    """
    metadata = record.get("metadata")
    if not isinstance(metadata, Mapping):
        return []
    raw = metadata.get("owned_paths")
    if not isinstance(raw, list):
        return []
    return sorted({entry for entry in raw if isinstance(entry, str) and entry})


def _project_ready(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": record["id"],
        "title": record.get("title"),
        "status": record.get("status"),
        "issue_type": record.get("issue_type"),
        "priority": record.get("priority"),
        "dependency_ids": _dependency_ids(record),
        "owned_paths": _owned_paths(record),
    }


def _project_blocked(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": record["id"],
        "blocked_by": list(record.get("blocked_by") or []),
        "blocked_by_count": record.get("blocked_by_count"),
    }


def _run(
    profile: str,
    arguments: Mapping[str, Any],
    *,
    repository_root: Path,
) -> safe_bd.SafeBdResult:
    request = safe_bd.SafeBdRequest(profile, arguments, repository_root, None)
    result = safe_bd.run_profile(request)
    if result.status != "ok":
        raise SnapshotCaptureError(
            "SNAPSHOT_CALL_FAILED",
            f"{profile}: status={result.status} error_code={result.error_code}",
        )
    return result


def capture_snapshot(
    *,
    repository_root: Path,
    root_issue_id: str | None = None,
    limit: int = _DEFAULT_READY_LIMIT,
    max_snapshot_bytes: int = MAX_SNAPSHOT_BYTES,
    clock: Callable[[], datetime] = _default_clock,
) -> dict[str, Any]:
    """Capture one internally-consistent, read-only Beads snapshot.

    Performs exactly five ``safe_bd.run_profile`` calls -- ``ready_list``,
    ``blocked_list``, ``dependency_cycles``, ``gate_list``, ``human_list``,
    every one of which is a non-mutating profile (verified by construction:
    ``safe_bd.PROFILES[<profile>].mutation is False`` for all five). Raises
    ``SnapshotCaptureError`` rather than returning a partial or best-effort
    result whenever any call fails, the observed workspace/CLI identity is
    inconsistent across calls (a same-capture TOCTOU signal), a result is
    malformed, or the serialized snapshot exceeds ``max_snapshot_bytes``.
    """
    ready_arguments: dict[str, Any] = {"sort": _READY_SORT, "limit": limit}
    if root_issue_id is not None:
        ready_arguments["parent"] = root_issue_id
    ready_result = _run("ready_list", ready_arguments, repository_root=repository_root)

    blocked_arguments: dict[str, Any] = {}
    if root_issue_id is not None:
        blocked_arguments["parent"] = root_issue_id
    blocked_result = _run(
        "blocked_list", blocked_arguments, repository_root=repository_root
    )

    cycles_result = _run("dependency_cycles", {}, repository_root=repository_root)
    gate_result = _run("gate_list", {}, repository_root=repository_root)
    human_result = _run("human_list", {}, repository_root=repository_root)

    results = (ready_result, blocked_result, cycles_result, gate_result, human_result)

    workspace_hashes = {r.workspace_sha256 for r in results}
    if len(workspace_hashes) != 1 or None in workspace_hashes:
        raise SnapshotCaptureError(
            "SNAPSHOT_WORKSPACE_MISMATCH",
            f"observed={sorted(str(h) for h in workspace_hashes)}",
        )

    cli_versions = {r.cli_version for r in results}
    if len(cli_versions) != 1:
        raise SnapshotCaptureError(
            "SNAPSHOT_CLI_VERSION_MISMATCH", f"observed={sorted(cli_versions)}"
        )

    if not isinstance(ready_result.data, list):
        raise SnapshotCaptureError("SNAPSHOT_MALFORMED", "ready_list.data not a list")
    ready = [_project_ready(record) for record in ready_result.data]

    if not isinstance(blocked_result.data, list):
        raise SnapshotCaptureError("SNAPSHOT_MALFORMED", "blocked_list.data not a list")
    blocked = [_project_blocked(record) for record in blocked_result.data]

    cycles_data = cycles_result.data
    if (
        not isinstance(cycles_data, Mapping)
        or not isinstance(cycles_data.get("cycles"), list)
        or not isinstance(cycles_data.get("count"), int)
    ):
        raise SnapshotCaptureError(
            "SNAPSHOT_MALFORMED", "dependency_cycles data malformed"
        )
    cycles = {"cycles": list(cycles_data["cycles"]), "count": cycles_data["count"]}

    if not isinstance(gate_result.data, list):
        raise SnapshotCaptureError("SNAPSHOT_MALFORMED", "gate_list.data not a list")
    gates = list(gate_result.data)

    if not isinstance(human_result.data, list):
        raise SnapshotCaptureError("SNAPSHOT_MALFORMED", "human_list.data not a list")
    human = list(human_result.data)

    captured_at = (
        clock().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
    )

    snapshot: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "captured_at": captured_at,
        "workspace_sha256": ready_result.workspace_sha256,
        "cli_version": ready_result.cli_version,
        "root_issue_id": root_issue_id,
        "ready": ready,
        "blocked": blocked,
        "cycles": cycles,
        "gates": gates,
        "human": human,
    }

    encoded = _canonical_json(snapshot).encode("utf-8")
    if len(encoded) > max_snapshot_bytes:
        raise SnapshotCaptureError(
            "SNAPSHOT_TOO_LARGE", f"{len(encoded)} > {max_snapshot_bytes}"
        )
    snapshot["content_sha256"] = hashlib.sha256(encoded).hexdigest()
    return snapshot


def _canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def persist_snapshot(snapshot: Mapping[str, Any], *, run_dir: Path) -> Path:
    """Atomically persist ``snapshot`` as ``snapshot.json`` under ``run_dir``.

    Delegates every filesystem-safety guarantee (owner-only directory
    creation, exclusive temp file, fsync, atomic rename, symlink/escape
    rejection) to ``coordinator_state``, matching the write-ahead pattern
    the rest of the Hermes-first coordinator uses.
    """
    owned_dir = state.ensure_owner_directory(Path(run_dir))
    encoded = _canonical_json(snapshot).encode("utf-8")
    return state.atomic_write(
        owned_dir / "snapshot.json",
        encoded,
        root=owned_dir,
        max_bytes=MAX_SNAPSHOT_BYTES,
    )
