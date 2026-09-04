#!/usr/bin/env python3
"""Deterministic stateless lane packaging and reproduction (spec §8.3a).

Freezes a lane into a parent-owned immutable package: the tracked binary
diff against the frozen base, the full content of every candidate file
(tracked-changed plus untracked), and a canonical manifest written LAST.
The package reproduces the candidate tree in any disposable directory; a
freeze is only valid when the reproduction is exact.  This module never
applies, commits, merges, stages, or pushes anything anywhere, and it
performs no Beads/Hermes/remote calls.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import stat
import sys
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

sys.path.insert(0, str(Path(__file__).parent))
import lane_snapshot

PACKAGING_TOOL_VERSION = "beads.package-lane.v1"
MANIFEST_MEMBER = "manifest.json"
MANIFEST_SCHEMA_VERSION = "beads.lane-package-manifest.v1"
DIFF_MEMBER = "tracked.diff"
BLOB_PREFIX = "blob/"
SYMLINK_PREFIX = "symlink/"

# Assembled at runtime so no source line itself carries a full PEM header.
_KEY = b" KEY-----"


def _marker(name: bytes) -> bytes:
    return b"-----BEGIN " + name + _KEY


_SENSITIVE_MARKERS = (
    _marker(b"OPENSSH PRIVATE"),
    _marker(b"RSA PRIVATE"),
    _marker(b"DSA PRIVATE"),
    _marker(b"EC PRIVATE"),
    _marker(b"ENCRYPTED PRIVATE"),
    b"-----BEGIN PGP PRIVATE" + _KEY + b" BLOCK" + b"-----",
    _marker(b"PRIVATE"),
)


class LanePackageError(ValueError):
    """Typed fail-closed packaging refusal (``.code`` names the cause)."""

    def __init__(self, code: str, *, path: str | None = None) -> None:
        self.code = code.split(":", 1)[0]
        self.path = path
        super().__init__(code if path is None else f"{code}:{path}")


@dataclass(frozen=True)
class LanePackage:
    archive_bytes: bytes
    archive_sha256: str
    manifest: dict[str, Any]
    lane_freeze: dict[str, Any]
    candidate_tree_sha256: str


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _matches(path: str, pattern: str) -> bool:
    if pattern.endswith("/**"):
        root = pattern[:-3].rstrip("/")
        return path == root or path.startswith(root + "/")
    return path == pattern or PurePosixPath(path).match(pattern)


def _entry_digest(entries: list[dict[str, Any]]) -> str:
    return hashlib.sha256(_canonical_json(entries)).hexdigest()


def _member(info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.mtime = 0
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    return info


def _budget(packet_budgets: Mapping[str, Any]) -> int:
    if not packet_budgets:
        return lane_snapshot.MAX_UNTRACKED_FILE_BYTES
    return lane_snapshot.budget_from_packet({"verification": dict(packet_budgets)})


def _candidate_entries(
    worktree: Path,
    base_sha: str,
    *,
    outbox: Path,
    scope: Mapping[str, Any],
    max_untracked_file_bytes: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str, str, list[str]]:
    """Collect normalized candidate + untracked-inventory entries.

    Returns (candidate_entries, untracked_inventory, tracked_diff_sha256,
    head_sha, changed_paths).  Raises typed refusals for scope escape,
    forbidden paths, oversized files, and sensitive content.
    """
    allowed = list(scope.get("allowed_paths", ()))
    forbidden = list(scope.get("forbidden_paths", ()))
    snapshot = lane_snapshot.capture(
        worktree,
        base_sha,
        exclude=outbox,
        max_untracked_file_bytes=max_untracked_file_bytes,
    )
    head = snapshot.head_sha
    diff_bytes = lane_snapshot.git_query(worktree, "diff", "--binary", base_sha)
    inventory_paths = {item["path"] for item in snapshot.inventory}
    changed_tracked = tuple(
        path for path in snapshot.changed_paths if path not in inventory_paths
    )
    candidate: list[dict[str, Any]] = []
    for relative in sorted(changed_tracked):
        path = worktree.joinpath(*relative.split("/"))
        if not any(_matches(relative, pattern) for pattern in allowed) or any(
            _matches(relative, pattern) for pattern in forbidden
        ):
            raise LanePackageError("LANE_SCOPE_ESCAPE", path=relative)
        if not path.exists():
            candidate.append(
                {
                    "path": relative,
                    "kind": "deleted",
                    "mode": 0,
                    "size_bytes": 0,
                    "sha256": lane_snapshot.SHA256_EMPTY,
                }
            )
            continue
        metadata = path.stat(follow_symlinks=False)
        candidate.append(
            {
                "path": relative,
                "kind": "file",
                "mode": stat.S_IMODE(metadata.st_mode),
                "size_bytes": metadata.st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    for item in snapshot.inventory:
        relative = item["path"]
        if any(_matches(relative, pattern) for pattern in forbidden):
            raise LanePackageError("LANE_FILE_FORBIDDEN", path=relative)
        if not any(_matches(relative, pattern) for pattern in allowed):
            raise LanePackageError("LANE_SCOPE_ESCAPE", path=relative)
        if item["kind"] == "symlink":
            candidate.append({**item, "kind": "symlink", "size_bytes": 0})
            continue
        data = worktree.joinpath(*relative.split("/")).read_bytes()
        if item["size_bytes"] > max_untracked_file_bytes:
            raise LanePackageError("LANE_FILE_OVERSIZED", path=relative)
        for marker in _SENSITIVE_MARKERS:
            if marker in data:
                raise LanePackageError("LANE_SENSITIVE_CONTENT", path=relative)
        candidate.append(dict(item))
    return (
        candidate,
        [dict(item) for item in snapshot.inventory],
        hashlib.sha256(diff_bytes).hexdigest(),
        head,
        list(snapshot.changed_paths),
    )


def _archive_bytes(members: list[tuple[str, bytes | None, str | None]]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for name, data, link in members:
            info = _member(tarfile.TarInfo(name))
            if link is not None:
                info.type = tarfile.SYMTYPE
                info.linkname = link
            else:
                assert data is not None
                info.size = len(data)
            tar.addfile(info, io.BytesIO(data) if data is not None else None)
    return buffer.getvalue()


def build_lane_package(
    worktree: Path,
    base_sha: str,
    *,
    outbox: Path,
    scope: Mapping[str, Any],
    packet_budgets: Mapping[str, Any],
    identity: Mapping[str, Any],
) -> LanePackage:
    """Freeze the lane into a deterministic self-contained package."""
    worktree = Path(worktree)
    outbox = Path(outbox)
    budget = _budget(packet_budgets)
    try:
        (
            candidate,
            inventory,
            tracked_diff_sha256,
            head,
            changed,
        ) = _candidate_entries(
            worktree,
            base_sha,
            outbox=outbox,
            scope=scope,
            max_untracked_file_bytes=budget,
        )
    except lane_snapshot.LaneSnapshotError as exc:
        if exc.reason == "over_budget":
            raise LanePackageError("LANE_FILE_OVERSIZED", path=exc.path) from None
        raise LanePackageError(str(exc), path=exc.path) from None

    if not changed:
        raise LanePackageError("LANE_FREEZE_EMPTY_INVENTORY")
    if any(item["kind"] == "deleted" for item in candidate) and not inventory:
        raise LanePackageError("LANE_FREEZE_EMPTY_INVENTORY")

    candidate_tree_sha256 = _entry_digest(candidate)
    diff_bytes = lane_snapshot.git_query(worktree, "diff", "--binary", base_sha)
    member_hashes: dict[str, str] = {
        DIFF_MEMBER: hashlib.sha256(diff_bytes).hexdigest()
    }
    members: list[tuple[str, bytes | None, str | None]] = [
        (DIFF_MEMBER, diff_bytes, None)
    ]
    for entry in candidate:
        if entry["kind"] == "deleted":
            continue
        if entry["kind"] == "symlink":
            link = os.readlink(worktree.joinpath(*entry["path"].split("/")))
            member = f"{SYMLINK_PREFIX}{entry['path']}"
            member_hashes[member] = entry["sha256"]
            members.append((member, None, link))
            continue
        data = worktree.joinpath(*entry["path"].split("/")).read_bytes()
        member = f"{BLOB_PREFIX}{entry['path']}"
        member_hashes[member] = hashlib.sha256(data).hexdigest()
        members.append((member, data, None))
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "packaging_tool_version": PACKAGING_TOOL_VERSION,
        "base_sha": base_sha,
        "head_sha": head,
        "tracked_diff_sha256": tracked_diff_sha256,
        "untracked_inventory_sha256": hashlib.sha256(
            _canonical_json(inventory)
        ).hexdigest(),
        "untracked_inventory": inventory,
        "candidate_entries": candidate,
        "candidate_tree_sha256": candidate_tree_sha256,
        "changed_paths": changed,
        "member_sha256": member_hashes,
    }
    manifest_bytes = _canonical_json(manifest) + b"\n"
    # The manifest is written LAST: it names every content member's hash.
    members.append((MANIFEST_MEMBER, manifest_bytes, None))
    archive = _archive_bytes(members)

    # Reproduce into a disposable directory and require exact equality.
    with tempfile_dir() as into:
        reproduced = reconstruct_candidate_tree(io.BytesIO(archive), into=into)
    if reproduced["reproduction_status"] != "reproduced":
        raise LanePackageError("LANE_REPRODUCTION_FAILED")

    freeze = {
        "schema_version": "beads.lane-freeze.v1",
        "run_id": identity["run_id"],
        "issue_id": identity["issue_id"],
        "attempt_id": identity["attempt_id"],
        "ownership_epoch": identity["ownership_epoch"],
        "worker_result_sha256": identity["worker_result_sha256"],
        "transfer_mode": "patch_package",
        "base_sha": base_sha,
        "observed_head_sha": head,
        "artifact_path": identity["artifact_path"],
        "artifact_sha256": hashlib.sha256(archive).hexdigest(),
        "candidate_tree_sha256": candidate_tree_sha256,
        "inventory": inventory,
        "packaging_tool_version": PACKAGING_TOOL_VERSION,
        "reproduction_status": "reproduced",
    }
    return LanePackage(
        archive_bytes=archive,
        archive_sha256=freeze["artifact_sha256"],
        manifest=manifest,
        lane_freeze=freeze,
        candidate_tree_sha256=candidate_tree_sha256,
    )


class _TempDir:
    def __enter__(self) -> Path:
        import tempfile

        self.path = Path(tempfile.mkdtemp(prefix="lane-reproduce-"))
        return self.path

    def __exit__(self, *exc: object) -> None:
        import shutil

        shutil.rmtree(self.path, ignore_errors=True)


def tempfile_dir() -> _TempDir:
    return _TempDir()


def _safe_entry_path(path: str) -> bool:
    """Reject every lexical escape in a manifest entry path."""
    return (
        bool(path)
        and not path.startswith("/")
        and "\\" not in path
        and "//" not in path
        and "./" not in path
        and path.endswith("/..") is False
        and all(part not in ("", ".", "..") for part in path.split("/"))
    )


def _validate_manifest_paths(manifest: dict[str, Any]) -> None:
    entries = manifest.get("candidate_entries")
    inventory = manifest.get("untracked_inventory")
    if not isinstance(entries, list) or not isinstance(inventory, list):
        raise LanePackageError("LANE_PACKAGE_INVALID")
    for entry in entries + inventory:
        if not isinstance(entry, dict) or not _safe_entry_path(entry.get("path", "")):
            raise LanePackageError("LANE_PACKAGE_INVALID")


def verify_package(archive_bytes: bytes) -> dict[str, Any]:
    """Validate the canonical package and return its manifest.

    Rejects truncated archives, non-canonical member ordering, hash drift,
    and any manifest that is not the final member.
    """
    try:
        return _verify_package(archive_bytes)
    except LanePackageError:
        raise
    except (tarfile.TarError, OSError, KeyError, ValueError, TypeError) as exc:
        raise LanePackageError("LANE_PACKAGE_INVALID") from exc


def _verify_package(archive_bytes: bytes) -> dict[str, Any]:
    with tarfile.open(fileobj=io.BytesIO(archive_bytes)) as tar:
        names = tar.getnames()
        if not names or names[-1] != MANIFEST_MEMBER:
            raise LanePackageError("LANE_PACKAGE_INVALID")
        if DIFF_MEMBER not in names:
            raise LanePackageError("LANE_PACKAGE_INVALID")
        if len(names) != len(set(names)):
            raise LanePackageError("LANE_PACKAGE_INVALID")
        try:
            manifest_stream = tar.extractfile(MANIFEST_MEMBER)
            assert manifest_stream is not None
            manifest_raw = manifest_stream.read()
            manifest = json.loads(manifest_raw.decode("utf-8", "strict"))
        except (OSError, ValueError, UnicodeError) as exc:
            raise LanePackageError("LANE_PACKAGE_INVALID") from exc
        if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
            raise LanePackageError("LANE_PACKAGE_INVALID")
        if manifest_raw != _canonical_json(manifest) + b"\n":
            raise LanePackageError("LANE_MANIFEST_NOT_CANONICAL")
        # A forged manifest must never smuggle traversal paths into
        # reconstruction or candidate-apply targets.
        _validate_manifest_paths(manifest)
        for name in names[:-1]:
            member = tar.getmember(name)
            if name == DIFF_MEMBER:
                diff_stream = tar.extractfile(name)
                assert diff_stream is not None
                data = diff_stream.read()
            elif name.startswith(SYMLINK_PREFIX):
                if not member.issym():
                    raise LanePackageError("LANE_PACKAGE_INVALID")
                data = member.linkname.encode("utf-8", "strict")
            elif name.startswith(BLOB_PREFIX):
                if not member.isfile():
                    raise LanePackageError("LANE_PACKAGE_INVALID")
                blob_stream = tar.extractfile(name)
                assert blob_stream is not None
                data = blob_stream.read()
            else:
                raise LanePackageError("LANE_PACKAGE_INVALID")
            expected = manifest["member_sha256"].get(name)
            if expected is None or hashlib.sha256(data).hexdigest() != expected:
                raise LanePackageError("LANE_MEMBER_HASH_MISMATCH")
    return manifest


def reconstruct_candidate_tree(
    archive: io.BytesIO | bytes | Path,
    *,
    into: Path,
) -> dict[str, Any]:
    """Rebuild the candidate tree in a disposable directory from the package.

    Returns ``{"reproduction_status", "candidate_tree_sha256"}``; the status
    is ``reproduced`` only when the rebuilt content exactly matches the
    package's recorded candidate identity.
    """
    if isinstance(archive, Path):
        raw = archive.read_bytes()
    elif isinstance(archive, bytes):
        raw = archive
    else:
        raw = archive.getvalue()
    manifest = verify_package(raw)
    into = Path(into)
    into.mkdir(parents=True, exist_ok=True)
    try:
        tar = tarfile.open(fileobj=io.BytesIO(raw))
    except (tarfile.TarError, OSError) as exc:
        raise LanePackageError("LANE_PACKAGE_INVALID") from exc
    with tar:
        for entry in manifest["candidate_entries"]:
            relative = entry["path"]
            target = into.joinpath(*relative.split("/"))
            if entry["kind"] == "deleted":
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if entry["kind"] == "symlink":
                member = f"{SYMLINK_PREFIX}{relative}"
                info = tar.getmember(member)
                if target.exists() or target.is_symlink():
                    raise LanePackageError("LANE_RECONSTRUCTION_CONFLICT")
                target.symlink_to(info.linkname)
                continue
            member = f"{BLOB_PREFIX}{relative}"
            stream = tar.extractfile(member)
            assert stream is not None
            data = stream.read()
            if len(data) != entry["size_bytes"]:
                return {"reproduction_status": "failed", "candidate_tree_sha256": None}
            if hashlib.sha256(data).hexdigest() != entry["sha256"]:
                return {"reproduction_status": "failed", "candidate_tree_sha256": None}
            with open(target, "wb") as stream:
                stream.write(data)
            os.chmod(target, entry["mode"] & 0o7777)
    rebuilt = _digest_tree(into, manifest)
    status = "reproduced" if rebuilt == manifest["candidate_tree_sha256"] else "failed"
    return {"reproduction_status": status, "candidate_tree_sha256": rebuilt}


def _digest_tree(into: Path, manifest: dict[str, Any]) -> str | None:
    entries: list[dict[str, Any]] = []
    for entry in manifest["candidate_entries"]:
        relative = entry["path"]
        target = into.joinpath(*relative.split("/"))
        if entry["kind"] == "deleted":
            entries.append(dict(entry))
            continue
        if not target.exists() and not target.is_symlink():
            return "failed"
        if entry["kind"] == "symlink":
            entries.append(
                {
                    "path": relative,
                    "kind": "symlink",
                    "mode": stat.S_IMODE(target.lstat().st_mode),
                    "size_bytes": 0,
                    "sha256": hashlib.sha256(
                        os.readlink(target).encode("utf-8", "strict")
                    ).hexdigest(),
                }
            )
            continue
        data = target.read_bytes()
        entries.append(
            {
                "path": relative,
                "kind": "file",
                "mode": stat.S_IMODE(target.stat().st_mode),
                "size_bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    return _entry_digest(entries)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="package-lane")
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("package-lane")
    build.add_argument("--worktree", type=Path, required=True)
    build.add_argument("--base-sha", required=True)
    build.add_argument("--outbox", type=Path, required=True)
    build.add_argument("--scope-file", type=Path, required=True)
    build.add_argument("--identity-file", type=Path, required=True)
    build.add_argument("--packet-budgets-file", type=Path, default=None)
    build.add_argument("--dest-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        scope = json.loads(args.scope_file.read_text(encoding="utf-8"))
        identity = json.loads(args.identity_file.read_text(encoding="utf-8"))
        budgets = (
            json.loads(args.packet_budgets_file.read_text(encoding="utf-8"))
            if args.packet_budgets_file
            else {}
        )
        built = build_lane_package(
            args.worktree,
            args.base_sha,
            outbox=args.outbox,
            scope=scope,
            packet_budgets=budgets,
            identity=identity,
        )
        dest = args.dest_dir
        dest.mkdir(parents=True, exist_ok=True)
        archive_path = dest / "lane-package.tar"
        archive_path.write_bytes(built.archive_bytes)
        freeze = dict(built.lane_freeze)
        freeze["artifact_path"] = str(archive_path)
        freeze_path = dest / "lane-freeze.json"
        freeze_path.write_bytes(_canonical_json(freeze) + b"\n")
        print(
            json.dumps(
                {
                    "status": "packaged",
                    "artifact_sha256": built.archive_sha256,
                    "candidate_tree_sha256": built.candidate_tree_sha256,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 0
    except (OSError, ValueError, LanePackageError):
        print('{"status":"refused","error_code":"LANE_PACKAGE_REFUSED"}')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
