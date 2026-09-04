#!/usr/bin/env python3
"""Shared canonical lane snapshot derived from the physical worktree.

Worker finalization and parent validation both recompute this snapshot from
the filesystem; neither side ever accepts lane state from the other.  Any
caller drift or post-finalization change therefore fails closed.
"""

from __future__ import annotations

import hashlib
import json
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

GIT_TIMEOUT_SECONDS = 60
MAX_UNTRACKED_FILE_BYTES = 10_485_760
SHA256_EMPTY = hashlib.sha256(b"").hexdigest()


class LaneSnapshotError(ValueError):
    """Typed fail-closed snapshot refusal."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


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


def capture(worktree: Path, base_sha: str, *, exclude: Path) -> LaneSnapshot:
    """Derive the canonical lane snapshot from the physical worktree."""
    worktree = Path(worktree)
    exclude = Path(exclude)
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
            if (
                path.is_symlink()
                or not stat.S_ISREG(metadata.st_mode)
                or metadata.st_size > MAX_UNTRACKED_FILE_BYTES
            ):
                raise LaneSnapshotError("LANE_SNAPSHOT_UNTRACKED_INVALID")
            data = path.read_bytes()
        except OSError as exc:
            raise LaneSnapshotError("LANE_SNAPSHOT_UNTRACKED_INVALID") from exc
        if len(data) != metadata.st_size:
            raise LaneSnapshotError("LANE_SNAPSHOT_UNTRACKED_INVALID")
        inventory.append(
            {
                "path": relative,
                "mode": stat.S_IMODE(metadata.st_mode),
                "size_bytes": metadata.st_size,
                "sha256": hashlib.sha256(data).hexdigest(),
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
    )
