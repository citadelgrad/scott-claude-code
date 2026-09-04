#!/usr/bin/env python3
"""Shared canonical lane snapshot derived from the physical worktree.

Worker finalization and parent validation both recompute this snapshot from
the filesystem; neither side ever accepts lane state from the other.  Any
caller drift or post-finalization change therefore fails closed.

Untracked symlinks are inventoried per ``lane-freeze-v1``: ``kind``
``"symlink"``, ``size_bytes`` 0, and a SHA-256 of the link text itself (no
target follow, so dangling links inventory identically).  Untracked-file
budgets are packet-declared through ``verification.max_untracked_file_bytes``
when present, defaulting to :data:`MAX_UNTRACKED_FILE_BYTES`.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

GIT_TIMEOUT_SECONDS = 60
MAX_UNTRACKED_FILE_BYTES = 10_485_760
MAX_U64 = (1 << 64) - 1
SHA256_EMPTY = hashlib.sha256(b"").hexdigest()


class LaneSnapshotError(ValueError):
    """Typed fail-closed snapshot refusal.

    ``path`` names the offending repo-relative path when the refusal is
    path-specific; the message is ``CODE:<path>:<reason>`` so operators can
    see exactly which file blocked the snapshot.
    """

    def __init__(self, code: str, *, path: str | None = None, reason: str = "") -> None:
        self.code = code
        self.path = path
        self.reason = reason
        message = (
            code
            if path is None
            else f"{code}:{path}:{reason}"
            if reason
            else f"{code}:{path}"
        )
        super().__init__(message)


def _git(worktree: Path, *args: str) -> bytes:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=worktree,
            capture_output=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise LaneSnapshotError("LANE_SNAPSHOT_GIT_FAILED") from exc
    return completed.stdout


def git_query(worktree: Path, *args: str) -> bytes:
    """Run one read-only Git query inside the worktree (shared helper)."""
    return _git(Path(worktree), *args)


def budget_from_packet(packet_value: Any) -> int:
    """Derive the untracked-file byte budget declared by a worker packet."""
    if not isinstance(packet_value, dict):
        raise LaneSnapshotError("LANE_SNAPSHOT_PACKET_INVALID")
    declared = packet_value.get("verification", {}).get("max_untracked_file_bytes")
    if declared is None:
        return MAX_UNTRACKED_FILE_BYTES
    if (
        isinstance(declared, bool)
        or not isinstance(declared, int)
        or not 1 <= declared <= MAX_U64
    ):
        raise LaneSnapshotError("LANE_SNAPSHOT_PACKET_INVALID")
    return declared


def _split_z(raw: bytes) -> list[str]:
    return [item.decode("utf-8", "strict") for item in raw.split(b"\x00") if item]


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


@dataclass(frozen=True)
class LaneSnapshot:
    head_sha: str
    tracked_diff_sha256: str
    untracked_inventory_sha256: str
    changed_paths: tuple[str, ...]
    dirty: bool
    inventory: tuple[dict[str, Any], ...] = field(default=())

    @property
    def lane_state(self) -> dict[str, Any]:
        return {
            "tracked_diff_sha256": self.tracked_diff_sha256,
            "untracked_inventory_sha256": self.untracked_inventory_sha256,
            "dirty": self.dirty,
        }

    @property
    def value(self) -> dict[str, Any]:
        return {
            "head_sha": self.head_sha,
            **self.lane_state,
            "changed_paths": list(self.changed_paths),
        }


def _is_excluded(path: Path, exclude: Path) -> bool:
    if exclude == path:
        return True
    try:
        path.relative_to(exclude)
    except ValueError:
        return False
    return True


def _invalid(relative: str, reason: str) -> LaneSnapshotError:
    return LaneSnapshotError(
        "LANE_SNAPSHOT_UNTRACKED_INVALID", path=relative, reason=reason
    )


def capture(
    worktree: Path,
    base_sha: str,
    *,
    exclude: Path,
    max_untracked_file_bytes: int | None = None,
) -> LaneSnapshot:
    """Derive the canonical lane snapshot from the physical worktree."""
    worktree = Path(worktree)
    exclude = Path(exclude)
    budget = (
        MAX_UNTRACKED_FILE_BYTES
        if max_untracked_file_bytes is None
        else max_untracked_file_bytes
    )
    if isinstance(budget, bool) or not isinstance(budget, int) or budget < 1:
        raise LaneSnapshotError("LANE_SNAPSHOT_PACKET_INVALID")
    head_raw = _git(worktree, "rev-parse", "HEAD")
    head = head_raw.decode("utf-8", "strict").strip()
    if not head:
        raise LaneSnapshotError("LANE_SNAPSHOT_GIT_FAILED")
    pathspec: list[str] = []
    try:
        exclude_relative = exclude.resolve(strict=True).relative_to(
            worktree.resolve(strict=True)
        )
    except (OSError, ValueError):
        exclude_relative = None
    if exclude_relative is not None:
        pathspec = [":(exclude)" + exclude_relative.as_posix()]
    diff_bytes = _git(worktree, "diff", "--binary", base_sha, "--", ".", *pathspec)
    changed_tracked = _split_z(
        _git(worktree, "diff", "--name-only", "-z", base_sha, "--", ".", *pathspec)
    )
    untracked = _split_z(
        _git(
            worktree,
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
            "--",
            ".",
            *pathspec,
        )
    )
    inventory: list[dict[str, Any]] = []
    for relative in sorted(untracked):
        path = worktree.joinpath(*relative.split("/"))
        if _is_excluded(path, exclude):
            continue
        try:
            metadata = path.stat(follow_symlinks=False)
        except OSError as exc:
            raise _invalid(relative, "stat_failed") from exc
        mode = stat.S_IMODE(metadata.st_mode)
        if path.is_symlink():
            # Hash the link text itself; never follow (dangling links are
            # inventoried identically to live ones).
            try:
                link_text = os.readlink(path)
            except OSError as exc:
                raise _invalid(relative, "readlink_failed") from exc
            try:
                encoded = link_text.encode("utf-8", "strict")
            except UnicodeEncodeError as exc:
                raise _invalid(relative, "link_not_utf8") from exc
            inventory.append(
                {
                    "path": relative,
                    "mode": mode,
                    "size_bytes": 0,
                    "sha256": hashlib.sha256(encoded).hexdigest(),
                    "kind": "symlink",
                }
            )
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise _invalid(relative, "not_regular_file")
        if metadata.st_size > budget:
            raise _invalid(relative, "over_budget")
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise _invalid(relative, "read_failed") from exc
        if len(data) != metadata.st_size or len(data) > budget:
            raise _invalid(relative, "size_changed")
        inventory.append(
            {
                "path": relative,
                "mode": mode,
                "size_bytes": metadata.st_size,
                "sha256": hashlib.sha256(data).hexdigest(),
                "kind": "file",
            }
        )
    changed = sorted(set(changed_tracked) | {item["path"] for item in inventory})
    return LaneSnapshot(
        head_sha=head,
        tracked_diff_sha256=hashlib.sha256(diff_bytes).hexdigest(),
        untracked_inventory_sha256=hashlib.sha256(
            _canonical_json(inventory)
        ).hexdigest(),
        changed_paths=tuple(changed),
        dirty=bool(changed),
        inventory=tuple(inventory),
    )
