#!/usr/bin/env python3
"""Canonical external-export package: deterministic archive and manifest.

A frozen external artifact is only valid if it is exactly this canonical
package: a deterministic tar archive whose first member is a canonical JSON
manifest binding the packet base SHA and the repo-relative inventory (modes,
sizes, content hashes), followed by the inventoried file contents.  The
manifest also carries the reconstructed candidate-tree digest over the
normalized inventory.  Arbitrary bytes are never a valid export.
"""

from __future__ import annotations

import hashlib
import io
import json
import stat
import tarfile
from typing import Any

MANIFEST_MEMBER = "manifest.json"
MANIFEST_SCHEMA_VERSION = "beads.external-export-manifest.v1"


class ExportPackageError(ValueError):
    """Typed fail-closed package refusal."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _safe_relative(path: str) -> bool:
    if (
        not path
        or path.startswith("/")
        or "\\" in path
        or path.startswith("./")
        or "//" in path
        or path.endswith("/..")
    ):
        return False
    parts = path.split("/")
    return all(part not in ("", ".", "..") for part in parts)


def _tree_digest(entries: list[dict[str, Any]]) -> str:
    return hashlib.sha256(_canonical_json(entries)).hexdigest()


def _member(info: tarfile.TarInfo, data: bytes) -> None:
    info.mtime = 0
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.type = tarfile.REGTYPE
    info.size = len(data)


def build_package(
    worktree: Any, base_sha: str, paths: list[str]
) -> tuple[bytes, dict[str, Any]]:
    """Build the canonical export package from worktree-relative paths."""
    entries: list[dict[str, Any]] = []
    blobs: list[tuple[dict[str, Any], bytes]] = []
    for relative in sorted(set(paths)):
        if not _safe_relative(relative):
            raise ExportPackageError("EXPORT_PATH_INVALID")
        path = worktree.joinpath(*relative.split("/"))
        metadata = path.stat(follow_symlinks=False)
        if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise ExportPackageError("EXPORT_PATH_INVALID")
        data = path.read_bytes()
        if len(data) != metadata.st_size:
            raise ExportPackageError("EXPORT_PATH_INVALID")
        entry = {
            "mode": stat.S_IMODE(metadata.st_mode),
            "path": relative,
            "sha256": hashlib.sha256(data).hexdigest(),
            "size_bytes": metadata.st_size,
        }
        entries.append(entry)
        blobs.append((entry, data))
    entries.sort(key=lambda item: item["path"])
    manifest = {
        "base_sha": base_sha,
        "entries": entries,
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "tree_sha256": _tree_digest(entries),
    }
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        manifest_bytes = _canonical_json(manifest) + b"\n"
        manifest_info = tarfile.TarInfo(MANIFEST_MEMBER)
        manifest_info.mode = 0o600
        _member(manifest_info, manifest_bytes)
        archive.addfile(manifest_info, io.BytesIO(manifest_bytes))
        for entry, data in blobs:
            info = tarfile.TarInfo(entry["path"])
            info.mode = entry["mode"]
            _member(info, data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue(), manifest


def verify_package(data: bytes, *, expected_base_sha: str) -> dict[str, Any]:
    """Verify the canonical package bytes and return the parsed manifest."""
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
            members = archive.getmembers()
            if not members or members[0].name != MANIFEST_MEMBER:
                raise ExportPackageError("EXPORT_PACKAGE_INVALID")
            stream = archive.extractfile(members[0])
            if stream is None:
                raise ExportPackageError("EXPORT_PACKAGE_INVALID")
            manifest_bytes = stream.read()
    except (OSError, ValueError, tarfile.TarError, EOFError):
        raise ExportPackageError("EXPORT_PACKAGE_INVALID") from None
    for member in members:
        if (
            (member.name != MANIFEST_MEMBER and not _safe_relative(member.name))
            or not member.isreg()
            or member.mtime != 0
        ):
            raise ExportPackageError("EXPORT_PACKAGE_INVALID")
    names = [member.name for member in members]
    if len(set(names)) != len(names) or names[1:] != sorted(names[1:]):
        raise ExportPackageError("EXPORT_PACKAGE_INVALID")
    try:
        manifest = json.loads(manifest_bytes)
    except ValueError:
        raise ExportPackageError("EXPORT_PACKAGE_INVALID") from None
    if (
        not isinstance(manifest, dict)
        or manifest_bytes != _canonical_json(manifest) + b"\n"
        or manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION
        or manifest.get("base_sha") != expected_base_sha
        or not isinstance(manifest.get("entries"), list)
    ):
        raise ExportPackageError("EXPORT_PACKAGE_INVALID")
    entries = manifest["entries"]
    if entries != sorted(entries, key=lambda item: item.get("path", "")):
        raise ExportPackageError("EXPORT_PACKAGE_INVALID")
    by_name = {member.name: member for member in members}
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"mode", "path", "sha256", "size_bytes"}
            or not _safe_relative(entry["path"])
            or not isinstance(entry["mode"], int)
            or not isinstance(entry["size_bytes"], int)
        ):
            raise ExportPackageError("EXPORT_PACKAGE_INVALID")
        member = by_name.get(entry["path"])
        if (
            member is None
            or member.size != entry["size_bytes"]
            or member.mode != entry["mode"]
        ):
            raise ExportPackageError("EXPORT_PACKAGE_INVALID")
    if manifest.get("tree_sha256") != _tree_digest(entries):
        raise ExportPackageError("EXPORT_PACKAGE_INVALID")
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
            for entry in entries:
                stream = archive.extractfile(entry["path"])
                if stream is None:
                    raise ExportPackageError("EXPORT_PACKAGE_INVALID")
                raw = stream.read()
                if (
                    len(raw) != entry["size_bytes"]
                    or hashlib.sha256(raw).hexdigest() != entry["sha256"]
                ):
                    raise ExportPackageError("EXPORT_PACKAGE_INVALID")
    except (OSError, ValueError, tarfile.TarError, EOFError, KeyError):
        raise ExportPackageError("EXPORT_PACKAGE_INVALID") from None
    return manifest
