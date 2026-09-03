#!/usr/bin/env python3
"""Durable local state primitives for the Hermes-first Beads coordinator.

This module is deliberately filesystem-only.  It never starts a subprocess and it
contains no Beads, Git, Hermes, Dolt, remote, or cleanup capability.
"""

from __future__ import annotations

import base64
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

import schema_runtime

FILE_MODE = 0o600
DIR_MODE = 0o700
MAX_U64 = (1 << 64) - 1
MAX_MANIFEST_BYTES = 65_536
MAX_RUN_BYTES = 104_857_600
GENESIS_SHA256 = "0" * 64
BASE32_RE = re.compile(r"^[A-Z2-7]{16}$")
RUN_ID_RE = re.compile(r"^run-[0-9a-f]{16}-[0-9]{8}T[0-9]{6}\.[0-9]{6}Z-[A-Z2-7]{8}$")


def _noop_hook(_event: str) -> None:
    pass


NOOP_HOOK: Callable[[str], None] = _noop_hook


class StateError(ValueError):
    """Typed fail-closed state error."""

    def __init__(self, code: str, *, status: str = "conflict") -> None:
        self.code = code
        self.status = status
        super().__init__(code)


@dataclass(frozen=True)
class JournalRead:
    records: tuple[dict[str, Any], ...]
    last_sha256: str
    torn_tail: bytes | None = None
    torn_offset: int | None = None


@dataclass(frozen=True)
class CheckpointRef:
    generation: int
    generation_path: Path
    generation_sha256: str
    value: dict[str, Any]


@dataclass(frozen=True)
class StartRunInput:
    request_id: str
    repository_root: str
    git_common_dir: str
    workspace: str
    run_root: str
    root_issue_id: str
    scope_issue_ids: tuple[str, ...]
    actor: str
    base_git_commit: str
    authority_snapshot_sha256: str


@dataclass(frozen=True)
class PointerObservation:
    classification: str
    state_sha256: str
    observed_value: dict[str, object] | None = None


@dataclass(frozen=True)
class PointerCallbacks:
    observe: Callable[[dict[str, object]], PointerObservation]
    publish: Callable[[dict[str, object]], PointerObservation]


@dataclass(frozen=True)
class BootstrapResult:
    disposition: str
    run_id: str
    run_directory: Path
    ownership_epoch: int | None
    checkpoint_generation: int | None


def canonical_payload_bytes(value: Any) -> bytes:
    """Return canonical JSON payload bytes without the persistence LF."""
    try:
        text = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        raw = text.encode("utf-8", "strict")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise StateError("CANONICAL_JSON_INVALID") from exc
    # Reparse through the frozen strict loader: this catches bool/int surprises,
    # unpaired surrogates, and any accidental future encoder drift.
    schema_runtime.strict_json_loads(raw, max_bytes=max(len(raw), 1))
    return raw


def canonical_bytes(value: Any) -> bytes:
    return canonical_payload_bytes(value) + b"\n"


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _opaque_utf8(value: str, *, label: str, maximum: int) -> bytes:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise StateError(f"{label}_INVALID")
    try:
        raw = value.encode("utf-8", "strict")
    except UnicodeEncodeError as exc:
        raise StateError(f"{label}_INVALID") from exc
    if len(raw) > maximum:
        raise StateError(f"{label}_TOO_LARGE")
    return raw


def issue_key(issue_id: str) -> str:
    return sha256_bytes(_opaque_utf8(issue_id, label="ISSUE_ID", maximum=1024))


def request_key(request_id: str) -> str:
    raw = _opaque_utf8(request_id, label="REQUEST_ID", maximum=128)
    if len(raw) < 16 or any(byte < 0x20 or byte > 0x7E for byte in raw):
        raise StateError("REQUEST_ID_INVALID")
    return sha256_bytes(raw)


def semantic_operation_id(identity: Mapping[str, Any]) -> str:
    required = {
        "schema",
        "effect_type",
        "target_identity",
        "immutable_input_sha256",
        "ownership_epoch",
    }
    if set(identity) != required:
        raise StateError("OPERATION_ID_INPUT_INVALID")
    if identity["ownership_epoch"] is not None and (
        type(identity["ownership_epoch"]) is not int
        or not 0 <= identity["ownership_epoch"] <= MAX_U64
    ):
        raise StateError("OPERATION_ID_INPUT_INVALID")
    for key in required - {"ownership_epoch"}:
        if not isinstance(identity[key], str) or not identity[key]:
            raise StateError("OPERATION_ID_INPUT_INVALID")
    digest = identity["immutable_input_sha256"]
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise StateError("OPERATION_ID_INPUT_INVALID")
    return sha256_bytes(canonical_payload_bytes(dict(identity)))


def utc_timestamp(value: Any) -> str:
    """Format a timezone-aware datetime with the schema's exact precision."""
    import datetime as dt

    if not isinstance(value, dt.datetime) or value.tzinfo is None:
        raise StateError("TIMESTAMP_INVALID")
    return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _lstat(path: Path) -> os.stat_result:
    try:
        return path.stat(follow_symlinks=False)
    except OSError as exc:
        raise StateError("PATH_STAT_FAILED") from exc


def _validate_owner(metadata: os.stat_result, *, mode: int, directory: bool) -> None:
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected_type(metadata.st_mode):
        raise StateError("PATH_TYPE_INVALID")
    if metadata.st_uid != os.getuid():
        raise StateError("PATH_OWNER_INVALID")
    if stat.S_IMODE(metadata.st_mode) != mode:
        raise StateError("PATH_MODE_INVALID")
    if not directory and metadata.st_nlink != 1:
        raise StateError("PATH_LINK_COUNT_INVALID")


def validate_owner_directory(
    path: Path | str, *, root: Path | str | None = None
) -> Path:
    target = Path(path)
    if root is not None:
        target = guarded_path(root, os.path.relpath(target, Path(root)))
    _validate_owner(_lstat(target), mode=DIR_MODE, directory=True)
    try:
        if target.resolve(strict=True) != target.absolute():
            raise StateError("PATH_ALIAS_INVALID")
    except OSError as exc:
        raise StateError("PATH_RESOLUTION_FAILED") from exc
    return target


def validate_owner_file(path: Path | str, *, root: Path | str) -> Path:
    target = Path(path)
    root_path = validate_owner_directory(Path(root))
    try:
        relative = target.absolute().relative_to(root_path.absolute())
    except ValueError as exc:
        raise StateError("PATH_OUTSIDE_ROOT") from exc
    checked = guarded_path(root_path, relative)
    _validate_owner(_lstat(checked), mode=FILE_MODE, directory=False)
    try:
        if checked.resolve(strict=True) != checked.absolute():
            raise StateError("PATH_ALIAS_INVALID")
    except OSError as exc:
        raise StateError("PATH_RESOLUTION_FAILED") from exc
    return checked


def guarded_path(
    root: Path | str, relative: Path | str, *, must_exist: bool = False
) -> Path:
    """Resolve a lexical child while rejecting every symlink/path escape."""
    root_path = Path(root)
    if not root_path.is_absolute():
        root_path = root_path.absolute()
    validate_metadata = _lstat(root_path)
    _validate_owner(validate_metadata, mode=DIR_MODE, directory=True)
    try:
        physical_root = root_path.resolve(strict=True)
    except OSError as exc:
        raise StateError("ROOT_RESOLUTION_FAILED") from exc
    if physical_root != root_path:
        raise StateError("ROOT_ALIAS_INVALID")
    child = Path(relative)
    if (
        child.is_absolute()
        or not child.parts
        or any(part in {"", ".", ".."} for part in child.parts)
    ):
        raise StateError("PATH_COMPONENT_INVALID")
    candidate = physical_root.joinpath(*child.parts)
    cursor = physical_root
    for index, part in enumerate(child.parts):
        cursor = cursor / part
        try:
            metadata = cursor.stat(follow_symlinks=False)
        except FileNotFoundError:
            if must_exist or index < len(child.parts) - 1:
                raise StateError("PATH_MISSING") from None
            break
        except OSError as exc:
            raise StateError("PATH_STAT_FAILED") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise StateError("PATH_ALIAS_INVALID")
        if index < len(child.parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
            raise StateError("PATH_TYPE_INVALID")
    try:
        candidate.absolute().relative_to(physical_root)
    except ValueError as exc:
        raise StateError("PATH_OUTSIDE_ROOT") from exc
    return candidate


def ensure_owner_directory(path: Path, *, root: Path | None = None) -> Path:
    if root is not None:
        try:
            relative = path.absolute().relative_to(root.absolute())
        except ValueError as exc:
            raise StateError("PATH_OUTSIDE_ROOT") from exc
        parent = root
        for part in relative.parts:
            candidate = guarded_path(parent, part)
            try:
                os.mkdir(candidate, DIR_MODE)
            except FileExistsError:
                pass
            validate_owner_directory(candidate, root=parent)
            parent = candidate
        return path
    try:
        os.mkdir(path, DIR_MODE)
    except FileExistsError:
        pass
    return validate_owner_directory(path)


def directory_fsync(path: Path, hook: Callable[[str], None] = NOOP_HOOK) -> None:
    fd = os.open(
        path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        os.fsync(fd)
        hook("after_directory_fsync")
    finally:
        os.close(fd)


def _exclusive_file(path: Path, mode: int = FILE_MODE) -> int:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(path, flags, mode)
    except OSError as exc:
        raise StateError("EXCLUSIVE_FILE_CREATE_FAILED") from exc


def atomic_write(
    path: Path,
    raw: bytes,
    *,
    root: Path,
    max_bytes: int,
    temp_name: str | None = None,
    hook: Callable[[str], None] = NOOP_HOOK,
) -> Path:
    if len(raw) > max_bytes:
        raise StateError("ARTIFACT_TOO_LARGE")
    target = guarded_path(root, path.absolute().relative_to(root.absolute()))
    if target.parent != root:
        validate_owner_directory(target.parent, root=root)
    name = temp_name or f".{target.name}.tmp"
    temp = guarded_path(target.parent, name)
    if temp.exists():
        existing = validate_owner_file(temp, root=root).read_bytes()
        if existing != raw:
            raise StateError("ATOMIC_TEMP_CONFLICT")
    else:
        fd = _exclusive_file(temp)
        try:
            view = memoryview(raw)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise StateError("FILE_WRITE_FAILED")
                view = view[written:]
            hook("after_temp_write")
            os.fsync(fd)
            hook("after_temp_fsync")
        except BaseException:
            os.close(fd)
            raise
        else:
            os.close(fd)
    try:
        os.replace(temp, target)
        hook("after_atomic_rename")
        directory_fsync(target.parent, hook)
    except OSError as exc:
        raise StateError("ATOMIC_PUBLICATION_FAILED") from exc
    return target


def create_owner_file(
    path: Path, raw: bytes = b"", *, root: Path, max_bytes: int = MAX_RUN_BYTES
) -> Path:
    if len(raw) > max_bytes:
        raise StateError("ARTIFACT_TOO_LARGE")
    target = guarded_path(root, path.absolute().relative_to(root.absolute()))
    fd = _exclusive_file(target)
    try:
        if raw:
            os.write(fd, raw)
        os.fsync(fd)
    finally:
        os.close(fd)
    directory_fsync(target.parent)
    return target


@contextlib.contextmanager
def exclusive_lock(
    path: Path,
    *,
    root: Path,
    blocking: bool = True,
    hook: Callable[[str], None] = NOOP_HOOK,
) -> Iterator[None]:
    """Acquire a same-host POSIX advisory lock and validate its backing file."""
    target = guarded_path(root, path.absolute().relative_to(root.absolute()))
    try:
        fd = os.open(
            target,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            FILE_MODE,
        )
    except OSError as exc:
        raise StateError("LOCK_OPEN_FAILED", status="unknown") from exc
    try:
        _validate_owner(os.fstat(fd), mode=FILE_MODE, directory=False)
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(fd, flags)
        except BlockingIOError as exc:
            raise StateError("LOCK_HELD", status="conflict") from exc
        except (OSError, AttributeError) as exc:
            raise StateError("LOCK_UNSUPPORTED", status="unknown") from exc
        hook("after_lock_acquire")
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
            hook("after_lock_release")
        except OSError:
            pass
        os.close(fd)


def binding_limits() -> dict[str, int]:
    return {
        "max_descendants": 100,
        "max_ready_fronts": 10,
        "max_worker_attempts_per_issue": 2,
        "max_nonprogress_rounds": 2,
        "max_manifest_bytes": 65_536,
        "max_receipt_bytes": 2_048,
        "max_pending_action_bytes": 16_384,
        "max_artifact_bytes": 10_485_760,
        "max_run_bytes": 104_857_600,
    }


def make_run_manifest(**values: Any) -> dict[str, Any]:
    manifest = {
        "schema_version": "beads.run-manifest.v1",
        "binding_limits": binding_limits(),
        **values,
    }
    schema_runtime.require_valid("run-manifest-v1.schema.json", manifest)
    if len(canonical_bytes(manifest)) > MAX_MANIFEST_BYTES:
        raise StateError("RUN_MANIFEST_TOO_LARGE")
    run_id = manifest.get("run_id")
    if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
        raise StateError("RUN_ID_INVALID")
    return manifest


def publish_run_manifest(
    run_directory: Path,
    manifest: Mapping[str, Any],
    *,
    crash_hook: Callable[[str], None] = NOOP_HOOK,
) -> Path:
    validate_owner_directory(run_directory)
    value = dict(manifest)
    schema_runtime.require_valid("run-manifest-v1.schema.json", value)
    if value["run_id"] != run_directory.name:
        raise StateError("RUN_MANIFEST_IDENTITY_MISMATCH")
    raw = canonical_bytes(value)
    target = run_directory / "run.json"
    if target.exists() or list(run_directory.glob("run.json.tmp-*")):
        recovered = recover_run_manifest(run_directory)
        if recovered.read_bytes() != raw:
            raise StateError("RUN_MANIFEST_IDENTITY_MISMATCH")
        return recovered
    suffix = base64.b32encode(secrets.token_bytes(10)).decode("ascii").rstrip("=")
    atomic_write(
        target,
        raw,
        root=run_directory,
        max_bytes=MAX_MANIFEST_BYTES,
        temp_name=f"run.json.tmp-{suffix}",
        hook=lambda event: crash_hook(
            "after_manifest_rename" if event == "after_atomic_rename" else event
        ),
    )
    return target


def load_run_manifest(run_directory: Path) -> dict[str, Any]:
    path = validate_owner_file(run_directory / "run.json", root=run_directory)
    raw = schema_runtime.read_bounded(path, MAX_MANIFEST_BYTES)
    value = schema_runtime.strict_json_loads(raw, max_bytes=MAX_MANIFEST_BYTES)
    schema_runtime.require_valid("run-manifest-v1.schema.json", value)
    if canonical_bytes(value) != raw or value["run_id"] != run_directory.name:
        raise StateError("RUN_MANIFEST_INVALID")
    return value


def recover_run_manifest(run_directory: Path) -> Path:
    validate_owner_directory(run_directory)
    final = run_directory / "run.json"
    temps = list(run_directory.glob("run.json.tmp-*"))
    if final.exists():
        validate_owner_file(final, root=run_directory)
        if temps:
            raise StateError("RUN_MANIFEST_TEMP_CONFLICT")
        load_run_manifest(run_directory)
        return final
    if len(temps) != 1:
        raise StateError(
            "RUN_MANIFEST_TEMP_CONFLICT", status="unknown" if not temps else "conflict"
        )
    temp = temps[0]
    suffix = temp.name.removeprefix("run.json.tmp-")
    if not BASE32_RE.fullmatch(suffix):
        raise StateError("RUN_MANIFEST_TEMP_CONFLICT")
    validate_owner_file(temp, root=run_directory)
    raw = schema_runtime.read_bounded(temp, MAX_MANIFEST_BYTES)
    value = schema_runtime.strict_json_loads(raw, max_bytes=MAX_MANIFEST_BYTES)
    schema_runtime.require_valid("run-manifest-v1.schema.json", value)
    if value["run_id"] != run_directory.name or raw != canonical_bytes(value):
        raise StateError("RUN_MANIFEST_TEMP_IDENTITY_MISMATCH")
    os.replace(temp, final)
    directory_fsync(run_directory)
    return final


def _read_canonical_lines(raw: bytes, schema_name: str) -> JournalRead:
    if not raw:
        return JournalRead((), GENESIS_SHA256)
    complete = raw.endswith(b"\n")
    parts = raw.splitlines(keepends=True)
    records: list[dict[str, Any]] = []
    previous = GENESIS_SHA256
    torn_tail: bytes | None = None
    torn_offset: int | None = None
    offset = 0
    for index, line in enumerate(parts):
        if not line.endswith(b"\n"):
            if index != len(parts) - 1:
                raise StateError("JOURNAL_INTERIOR_CORRUPT")
            torn_tail, torn_offset = line, offset
            break
        try:
            value = schema_runtime.strict_json_loads(
                line[:-1], max_bytes=MAX_MANIFEST_BYTES
            )
            schema_runtime.require_valid(schema_name, value)
        except Exception as exc:
            raise StateError("JOURNAL_INTERIOR_CORRUPT") from exc
        if canonical_bytes(value) != line or value["previous_event_sha256"] != previous:
            raise StateError("JOURNAL_HASH_CHAIN_INVALID")
        records.append(value)
        previous = sha256_bytes(line)
        offset += len(line)
    if complete:
        torn_tail = None
        torn_offset = None
    return JournalRead(tuple(records), previous, torn_tail, torn_offset)


class OperationJournal:
    def __init__(self, run_directory: Path) -> None:
        self.run_directory = validate_owner_directory(run_directory)
        self.path = self.run_directory / "operations.jsonl"

    @classmethod
    def create(cls, run_directory: Path) -> "OperationJournal":
        journal = cls(run_directory)
        if journal.path.exists():
            validate_owner_file(journal.path, root=run_directory)
        else:
            create_owner_file(journal.path, root=run_directory)
        return journal

    def read(self) -> JournalRead:
        validate_owner_file(self.path, root=self.run_directory)
        with self.path.open("rb") as stream:
            raw = stream.read(MAX_RUN_BYTES + 1)
        if len(raw) > MAX_RUN_BYTES:
            raise StateError("JOURNAL_TOO_LARGE")
        return _read_canonical_lines(raw, "operation-journal-event-v1.schema.json")

    def append(
        self,
        event: Mapping[str, Any],
        *,
        hook: Callable[[str], None] = NOOP_HOOK,
    ) -> dict[str, Any]:
        state = self.read()
        if state.torn_tail is not None:
            raise StateError("JOURNAL_TORN_TAIL", status="unknown")
        candidate = dict(event)
        operation_id = candidate.get("operation_id")
        same_id = [
            record for record in state.records if record["operation_id"] == operation_id
        ]
        if same_id:
            same_phase = [
                record
                for record in same_id
                if record["phase"] == candidate.get("phase")
            ]
            if same_phase:
                original = same_phase[0]
                comparable = dict(candidate)
                comparable["previous_event_sha256"] = original["previous_event_sha256"]
                if comparable == original:
                    return original
                raise StateError("OPERATION_ID_REUSE_CONFLICT")
            phases = {record["phase"] for record in same_id}
            if candidate.get("phase") != "RESOLUTION" or phases != {"PREPARED"}:
                raise StateError("OPERATION_ID_REUSE_CONFLICT")
        candidate["previous_event_sha256"] = state.last_sha256
        schema_runtime.require_valid(
            "operation-journal-event-v1.schema.json", candidate
        )
        raw = canonical_bytes(candidate)
        fd = os.open(
            self.path, os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            os.write(fd, raw)
            hook("after_journal_write")
            os.fsync(fd)
            hook("after_journal_fsync")
        finally:
            os.close(fd)
        return candidate


def _filesystem_probe(path: str, digest: str) -> dict[str, Any]:
    return {
        "schema_version": "beads.recovery-probe.v1",
        "kind": "request",
        "probe_type": "filesystem_identity",
        "target_identity": path,
        "expected_before_sha256": GENESIS_SHA256,
        "intended_after_identity": path,
        "intended_after_sha256": digest,
        "descriptor": ["filesystem", "checkpoint"],
        "timeout_seconds": 1,
        "required_authority": "local_write",
    }


class CheckpointStore:
    def __init__(self, run_directory: Path, journal: OperationJournal) -> None:
        self.run_directory = validate_owner_directory(run_directory)
        self.journal = journal
        if journal.run_directory != self.run_directory:
            raise StateError("JOURNAL_RUN_MISMATCH")
        self.directory = self.run_directory / "checkpoints"
        ensure_owner_directory(self.directory, root=self.run_directory)

    def _accepted(self) -> list[dict[str, Any]]:
        return [
            record
            for record in self.journal.read().records
            if record["phase"] == "CHECKPOINT_ACCEPTED"
        ]

    def accept(
        self,
        checkpoint: Mapping[str, Any],
        *,
        hook: Callable[[str], None] = NOOP_HOOK,
    ) -> CheckpointRef:
        value = dict(checkpoint)
        schema_runtime.require_valid("run-checkpoint-v1.schema.json", value)
        generation = value["generation"]
        if value["run_id"] != self.run_directory.name:
            raise StateError("CHECKPOINT_RUN_MISMATCH")
        accepted = self._accepted()
        raw = canonical_bytes(value)
        digest = sha256_bytes(raw)
        if accepted and accepted[-1]["generation"] == generation:
            event = accepted[-1]
            if event["checkpoint_sha256"] != digest:
                raise StateError("CHECKPOINT_GENERATION_COLLISION")
            path = Path(event["checkpoint_path"])
            if validate_owner_file(path, root=self.run_directory).read_bytes() != raw:
                raise StateError("ACCEPTED_CHECKPOINT_HASH_MISMATCH")
            current = self.current(rebuild_pointer=False)
            if current.generation != generation or current.generation_sha256 != digest:
                raise StateError("CHECKPOINT_POINTER_STALE", status="unknown")
            return CheckpointRef(generation, path, digest, value)
        expected = (accepted[-1]["generation"] + 1) if accepted else 1
        if generation != expected:
            raise StateError("CHECKPOINT_GENERATION_INVALID")
        previous = accepted[-1]["checkpoint_sha256"] if accepted else GENESIS_SHA256
        if value["previous_checkpoint_sha256"] != previous:
            raise StateError("CHECKPOINT_PREDECESSOR_INVALID")
        path = self.directory / f"{generation:06d}.json"
        if path.exists():
            existing = validate_owner_file(path, root=self.run_directory).read_bytes()
            if existing != raw:
                raise StateError("CHECKPOINT_GENERATION_COLLISION")
        else:
            atomic_write(
                path,
                raw,
                root=self.run_directory,
                max_bytes=MAX_MANIFEST_BYTES,
                hook=hook,
            )
        digest = sha256_bytes(raw)
        identity = {
            "schema": "beads.checkpoint-acceptance.v1",
            "effect_type": "CHECKPOINT_ACCEPT",
            "target_identity": str(path.resolve()),
            "immutable_input_sha256": digest,
            "ownership_epoch": None,
        }
        event = {
            "schema_version": "beads.operation-journal-event.v1",
            "operation_id": semantic_operation_id(identity),
            "run_id": self.run_directory.name,
            "attempt_id": None,
            "issue_id": None,
            "ownership_epoch": None,
            "effect_type": "CHECKPOINT_ACCEPT",
            "immutable_input_path": str(path.resolve()),
            "immutable_input_sha256": digest,
            "expected_pre_state_sha256": previous,
            "recovery_probe": _filesystem_probe(str(path.resolve()), digest),
            "timestamp": value["created_at"],
            "authority_class": "local_write",
            "previous_event_sha256": GENESIS_SHA256,
            "phase": "CHECKPOINT_ACCEPTED",
            "generation": generation,
            "checkpoint_path": str(path.resolve()),
            "checkpoint_sha256": digest,
        }
        self.journal.append(event, hook=hook)
        hook("after_checkpoint_acceptance")
        self._write_pointer(generation, path, digest, hook=hook)
        return CheckpointRef(generation, path, digest, value)

    def _write_pointer(
        self,
        generation: int,
        path: Path,
        digest: str,
        *,
        hook: Callable[[str], None] = NOOP_HOOK,
    ) -> None:
        pointer = {
            "schema_version": "beads.checkpoint-pointer.v1",
            "run_id": self.run_directory.name,
            "generation": generation,
            "generation_path": f"checkpoints/{path.name}",
            "generation_sha256": digest,
        }
        schema_runtime.require_valid("checkpoint-pointer-v1.schema.json", pointer)
        atomic_write(
            self.directory / "latest.json",
            canonical_bytes(pointer),
            root=self.run_directory,
            max_bytes=4096,
            hook=hook,
        )
        hook("after_pointer_publication")

    def current(self, *, rebuild_pointer: bool = False) -> CheckpointRef:
        accepted = self._accepted()
        if not accepted:
            raise StateError("NO_ACCEPTED_CHECKPOINT", status="unknown")
        event = accepted[-1]
        path = Path(event["checkpoint_path"])
        try:
            validate_owner_file(path, root=self.run_directory)
            raw = path.read_bytes()
        except (OSError, StateError) as exc:
            raise StateError("ACCEPTED_CHECKPOINT_MISSING") from exc
        digest = sha256_bytes(raw)
        if digest != event["checkpoint_sha256"]:
            raise StateError("ACCEPTED_CHECKPOINT_HASH_MISMATCH")
        value = schema_runtime.strict_json_loads(raw, max_bytes=MAX_MANIFEST_BYTES)
        schema_runtime.require_valid("run-checkpoint-v1.schema.json", value)
        if canonical_bytes(value) != raw or value["generation"] != event["generation"]:
            raise StateError("ACCEPTED_CHECKPOINT_INVALID")
        reference = CheckpointRef(event["generation"], path, digest, value)
        pointer_path = self.directory / "latest.json"
        pointer_ok = False
        try:
            pointer_raw = validate_owner_file(
                pointer_path, root=self.run_directory
            ).read_bytes()
            pointer = schema_runtime.strict_json_loads(pointer_raw, max_bytes=4096)
            schema_runtime.require_valid("checkpoint-pointer-v1.schema.json", pointer)
            pointer_ok = (
                canonical_bytes(pointer) == pointer_raw
                and pointer["run_id"] == self.run_directory.name
                and pointer["generation"] == reference.generation
                and pointer["generation_sha256"] == reference.generation_sha256
                and pointer["generation_path"] == f"checkpoints/{path.name}"
            )
        except (
            OSError,
            StateError,
            schema_runtime.JsonLoadFailure,
            schema_runtime.ValidationFailure,
        ):
            pointer_ok = False
        if not pointer_ok:
            if not rebuild_pointer:
                raise StateError("CHECKPOINT_POINTER_STALE", status="unknown")
            self._write_pointer(reference.generation, path, digest)
        return reference


def run_size(run_directory: Path) -> int:
    validate_owner_directory(run_directory)
    total = 0
    for current, directories, files in os.walk(run_directory, followlinks=False):
        current_path = Path(current)
        for name in directories:
            metadata = _lstat(current_path / name)
            if stat.S_ISLNK(metadata.st_mode):
                raise StateError("PATH_ALIAS_INVALID")
        for name in files:
            path = current_path / name
            metadata = _lstat(path)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise StateError("PATH_LINK_COUNT_INVALID")
            total += metadata.st_size
            if total > MAX_RUN_BYTES:
                raise StateError("RUN_SIZE_LIMIT_EXCEEDED")
    return total


def _validate_start_input(request: StartRunInput) -> dict[str, Any]:
    request_key(request.request_id)
    _opaque_utf8(request.root_issue_id, label="ROOT_ISSUE_ID", maximum=1024)
    _opaque_utf8(request.actor, label="ACTOR", maximum=4096)
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", request.base_git_commit):
        raise StateError("BASE_GIT_COMMIT_INVALID")
    if not re.fullmatch(r"[0-9a-f]{64}", request.authority_snapshot_sha256):
        raise StateError("AUTHORITY_SNAPSHOT_INVALID")
    physical: dict[str, str] = {}
    for field in ("repository_root", "git_common_dir", "workspace", "run_root"):
        supplied = Path(getattr(request, field))
        if not supplied.is_absolute() or supplied.resolve() != supplied:
            raise StateError("START_PATH_NOT_PHYSICAL")
        physical[field] = str(supplied)
    run_root = Path(physical["run_root"])
    validate_owner_directory(run_root)
    repository = Path(physical["repository_root"])
    try:
        run_root.relative_to(repository)
    except ValueError as exc:
        raise StateError("RUN_ROOT_OUTSIDE_REPOSITORY") from exc
    scope = sorted(
        {
            _opaque_utf8(issue, label="SCOPE_ISSUE_ID", maximum=1024): issue
            for issue in request.scope_issue_ids
        }.items()
    )
    scope_ids = [value for _raw, value in scope]
    if request.root_issue_id in scope_ids:
        raise StateError("ROOT_ISSUE_IN_WRITING_SCOPE")
    if len(scope_ids) > 100:
        raise StateError("SCOPE_TOO_LARGE")
    return {
        "schema_version": "beads.start-input.v1",
        "request_id": request.request_id,
        **physical,
        "root_issue_id": request.root_issue_id,
        "scope_issue_ids": scope_ids,
        "actor": request.actor,
        "base_git_commit": request.base_git_commit,
        "authority_snapshot_sha256": request.authority_snapshot_sha256,
        "binding_limits": binding_limits(),
    }


def _default_run_id(root_issue_id: str, timestamp: str) -> str:
    diagnostic = issue_key(root_issue_id)[:16]
    compact = timestamp.replace("-", "").replace(":", "")
    suffix = base64.b32encode(secrets.token_bytes(5)).decode("ascii")
    return f"run-{diagnostic}-{compact}-{suffix}"


def _read_request_record(path: Path, requests_root: Path) -> dict[str, Any]:
    raw = validate_owner_file(path, root=requests_root.parent).read_bytes()
    value = schema_runtime.strict_json_loads(raw, max_bytes=MAX_MANIFEST_BYTES)
    schema_runtime.require_valid("run-request-v1.schema.json", value)
    if raw != canonical_bytes(value):
        raise StateError("REQUEST_RECORD_INVALID")
    return value


def _write_request_record(
    path: Path, requests_root: Path, value: dict[str, Any], hook: Callable[[str], None]
) -> None:
    schema_runtime.require_valid("run-request-v1.schema.json", value)
    atomic_write(
        path,
        canonical_bytes(value),
        root=requests_root.parent,
        max_bytes=MAX_MANIFEST_BYTES,
        hook=hook,
    )
    hook("after_request_publication")


def _bootstrap_probe(
    target: str, before: str, after: str, probe_type: str = "filesystem_identity"
) -> dict[str, Any]:
    return {
        "schema_version": "beads.recovery-probe.v1",
        "kind": "request",
        "probe_type": probe_type,
        "target_identity": target,
        "expected_before_sha256": before,
        "intended_after_identity": target,
        "intended_after_sha256": after,
        "descriptor": [probe_type, "bootstrap"],
        "timeout_seconds": 30,
        "required_authority": "coordinator_parent",
    }


def _prepared_event(
    *,
    run: Path,
    operation_id: str,
    effect_type: str,
    input_path: Path,
    input_sha: str,
    expected_sha: str,
    probe: dict[str, Any],
    timestamp: str,
    issue_id: str | None,
    epoch: int | None,
) -> dict[str, Any]:
    return {
        "schema_version": "beads.operation-journal-event.v1",
        "operation_id": operation_id,
        "run_id": run.name,
        "attempt_id": None,
        "issue_id": issue_id,
        "ownership_epoch": epoch,
        "effect_type": effect_type,
        "immutable_input_path": str(input_path.resolve()),
        "immutable_input_sha256": input_sha,
        "expected_pre_state_sha256": expected_sha,
        "recovery_probe": probe,
        "timestamp": timestamp,
        "authority_class": "coordinator_parent",
        "previous_event_sha256": GENESIS_SHA256,
        "phase": "PREPARED",
    }


def _resolution_event(
    prepared: dict[str, Any],
    *,
    status: str,
    observed_sha: str | None,
    evidence_path: Path | None,
    evidence_sha: str | None,
    timestamp: str,
) -> dict[str, Any]:
    return {
        **prepared,
        "phase": "RESOLUTION",
        "timestamp": timestamp,
        "observed_post_state_sha256": observed_sha,
        "readback_evidence_path": str(evidence_path.resolve())
        if evidence_path
        else None,
        "readback_evidence_sha256": evidence_sha,
        "status": status,
        "error": None
        if status in {"APPLIED", "NOT_APPLIED"}
        else {
            "code": f"BOOTSTRAP_{status}",
            "template_id": "bootstrap_effect_not_applied",
            "field_path": "/bootstrap",
            "parameters": [],
        },
    }


def _empty_bootstrap_checkpoint(
    run: Path,
    manifest: dict[str, Any],
    generation: int,
    previous: str,
    ownership: dict[str, Any],
    timestamp: str,
    authority_snapshot_sha256: str,
) -> dict[str, Any]:
    issue = {
        "tracker_status_observed": "coordination_root",
        "readiness": {"state": "not_ready", "reason": "non-writing coordination root"},
        "ownership": {
            "state": "held",
            "epoch": ownership["epoch"],
            "token_sha256": ownership["token_sha256"],
            "actor": ownership["actor"],
        },
        "attempt": {
            "state": "not_started",
            "attempt_id": None,
            "delegation_id": None,
            "subagent_id": None,
            "child_session_id": None,
        },
        "worker_result": {"state": "absent", "outcome": None, "record_sha256": None},
        "artifact": {"state": "none", "lane_freeze_sha256": None},
        "verification": {"state": "not_started", "record_sha256": None},
        "review": {"state": "not_required", "record_sha256": None},
        "integration": {
            "state": "not_started",
            "candidate_sha256": None,
            "event_id": None,
        },
        "gate": {"state": "none", "gate_id": None},
        "packet_sha256": None,
        "result_sha256": None,
        "worktree": None,
        "branch": None,
        "base_sha": None,
        "head_sha": None,
    }
    return {
        "schema_version": "beads.run-checkpoint.v1",
        "run_id": run.name,
        "generation": generation,
        "root_issue_id": manifest["root_issue_id"],
        "workspace": manifest["workspace"],
        "repository_root": manifest["repository_root"],
        "coordinator_session_id": None,
        "authority_snapshot_sha256": authority_snapshot_sha256,
        "phase": "bootstrap_pointer" if generation == 1 else "active",
        "budget": {
            "max_parallel": 3,
            "max_ready_fronts": 10,
            "max_worker_attempts_per_issue": 2,
            "max_nonprogress_rounds": 2,
        },
        "issues": {manifest["root_issue_id"]: issue},
        "operation_journal_path": str((run / "operations.jsonl").resolve()),
        "issue_snapshot_sha256": "0" * 64,
        "ready_front_sha256": "0" * 64,
        "previous_checkpoint_sha256": previous,
        "created_at": timestamp,
    }


def bootstrap_run(
    request: StartRunInput,
    callbacks: PointerCallbacks,
    *,
    now: str,
    run_id_factory: Callable[[], str] | None = None,
    secret_factory: Callable[[], bytes] = lambda: secrets.token_bytes(32),
    crash_hook: Callable[[str], None] = NOOP_HOOK,
) -> BootstrapResult:
    """Execute the Task-8 half of bootstrap through typed pointer callbacks."""
    start_input = _validate_start_input(request)
    start_sha = sha256_bytes(canonical_payload_bytes(start_input))
    root = Path(request.run_root)
    requests_root = root / "_requests"
    ensure_owner_directory(requests_root, root=root)
    mapping_path = requests_root / f"{request_key(request.request_id)}.json"
    with exclusive_lock(requests_root / ".lock", root=root, hook=crash_hook):
        if mapping_path.exists():
            mapping = _read_request_record(mapping_path, requests_root)
            if (
                mapping["request_id"] != request.request_id
                or mapping["start_input_v1_sha256"] != start_sha
            ):
                raise StateError("REQUEST_MAPPING_CONFLICT")
        else:
            run_id = (
                run_id_factory or (lambda: _default_run_id(request.root_issue_id, now))
            )()
            if not RUN_ID_RE.fullmatch(run_id):
                raise StateError("RUN_ID_INVALID")
            if (root / run_id).exists():
                raise StateError("RUN_ID_COLLISION")
            mapping = {
                "schema_version": "beads.run-request.v1",
                "request_key": request_key(request.request_id),
                "request_id": request.request_id,
                "start_input_v1_sha256": start_sha,
                "run_id": run_id,
                "status": "allocating",
            }
            _write_request_record(mapping_path, requests_root, mapping, crash_hook)
    run = root / mapping["run_id"]
    if not run.exists():
        os.mkdir(run, DIR_MODE)
        crash_hook("after_run_directory_create")
        directory_fsync(root, crash_hook)
    validate_owner_directory(run, root=root)
    with exclusive_lock(run / "run.lock", root=run, hook=crash_hook):
        if not (run / "run.json").exists() and not list(run.glob("run.json.tmp-*")):
            secret = secret_factory()
            if not isinstance(secret, bytes) or len(secret) != 32:
                raise StateError("RUN_SECRET_INVALID")
            manifest = make_run_manifest(
                run_id=run.name,
                request_id=request.request_id,
                repository_root=request.repository_root,
                workspace=request.workspace,
                root_issue_id=request.root_issue_id,
                created_at=now,
                coordinator_version="beads-coordinator-v1",
                run_root=request.run_root,
                run_secret_hex=secret.hex(),
            )
            publish_run_manifest(run, manifest, crash_hook=crash_hook)
        else:
            recover_run_manifest(run)
            manifest = load_run_manifest(run)
        journal = OperationJournal.create(run)
        import beads_ownership

        owner_store = beads_ownership.OwnershipStore(root, crash_hook=crash_hook)
        inspected = owner_store.inspect(request.root_issue_id)
        if (
            inspected.disposition == "held"
            and inspected.record
            and inspected.record["run_id"] == run.name
        ):
            owned = inspected
            records = journal.read().records
            resolved_ids = {
                item["operation_id"]
                for item in records
                if item["phase"] == "RESOLUTION"
            }
            dangling_acquire = next(
                (
                    item
                    for item in records
                    if item["phase"] == "PREPARED"
                    and item["effect_type"] == "OWNERSHIP_ACQUIRE"
                    and item["operation_id"] not in resolved_ids
                ),
                None,
            )
            if dangling_acquire is not None:
                owner_path = (
                    root
                    / "_ownership"
                    / issue_key(request.root_issue_id)
                    / "current.json"
                )
                owner_evidence = validate_owner_file(owner_path, root=root)
                evidence_sha = sha256_bytes(owner_evidence.read_bytes())
                journal.append(
                    _resolution_event(
                        dangling_acquire,
                        status="APPLIED",
                        observed_sha=evidence_sha,
                        evidence_path=owner_evidence,
                        evidence_sha=evidence_sha,
                        timestamp=now,
                    ),
                    hook=crash_hook,
                )
        elif inspected.disposition == "unheld" or inspected.disposition == "released":
            proposed_epoch = (inspected.record["epoch"] + 1) if inspected.record else 1
            owner_input = {
                "schema_version": "beads.ownership-acquire-input.v1",
                "issue_id": request.root_issue_id,
                "actor": request.actor,
                "run_id": run.name,
                "epoch": proposed_epoch,
                "tracker_state_sha256": "0" * 64,
            }
            owner_input_path = run / "bootstrap-ownership-input.json"
            owner_raw = canonical_bytes(owner_input)
            if owner_input_path.exists():
                if (
                    validate_owner_file(owner_input_path, root=run).read_bytes()
                    != owner_raw
                ):
                    raise StateError("BOOTSTRAP_INPUT_CONFLICT")
            else:
                atomic_write(
                    owner_input_path,
                    owner_raw,
                    root=run,
                    max_bytes=MAX_MANIFEST_BYTES,
                    hook=crash_hook,
                )
            owner_sha = sha256_bytes(owner_raw)
            operation_id = semantic_operation_id(
                {
                    "schema": "beads.ownership-acquire-input.v1",
                    "effect_type": "OWNERSHIP_ACQUIRE",
                    "target_identity": request.root_issue_id,
                    "immutable_input_sha256": owner_sha,
                    "ownership_epoch": proposed_epoch,
                }
            )
            owner_path = (
                root / "_ownership" / issue_key(request.root_issue_id) / "current.json"
            )
            prepared = _prepared_event(
                run=run,
                operation_id=operation_id,
                effect_type="OWNERSHIP_ACQUIRE",
                input_path=owner_input_path,
                input_sha=owner_sha,
                expected_sha="0" * 64,
                probe=_bootstrap_probe(str(owner_path.resolve()), "0" * 64, owner_sha),
                timestamp=now,
                issue_id=request.root_issue_id,
                epoch=proposed_epoch,
            )
            journal.append(prepared, hook=crash_hook)
            owned = owner_store.acquire(
                issue_id=request.root_issue_id,
                actor=request.actor,
                run_directory=run,
                tracker_state_sha256="0" * 64,
                operation_id=operation_id,
                now=dt.datetime.strptime(now, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
                    tzinfo=dt.timezone.utc
                ),
            )
            if owned.disposition != "held" or owned.record is None:
                return BootstrapResult("conflict", run.name, run, None, None)
            owner_evidence = validate_owner_file(owner_path, root=root)
            evidence_sha = sha256_bytes(owner_evidence.read_bytes())
            journal.append(
                _resolution_event(
                    prepared,
                    status="APPLIED",
                    observed_sha=evidence_sha,
                    evidence_path=owner_evidence,
                    evidence_sha=evidence_sha,
                    timestamp=now,
                ),
                hook=crash_hook,
            )
        else:
            return BootstrapResult(inspected.disposition, run.name, run, None, None)
        assert owned.record is not None
        store = CheckpointStore(run, journal)
        accepted = store._accepted()
        if not accepted:
            first_value = _empty_bootstrap_checkpoint(
                run,
                manifest,
                1,
                GENESIS_SHA256,
                owned.record,
                now,
                request.authority_snapshot_sha256,
            )
            first = store.accept(first_value, hook=crash_hook)
        else:
            first = store.current(rebuild_pointer=True)
        if first.generation >= 2:
            expected = {
                "schema_version": "beads.run-pointer.v1",
                "run_id": run.name,
                "checkpoint_generation": 1,
                "checkpoint_sha256": first.value["previous_checkpoint_sha256"]
                if first.generation == 2
                else first.generation_sha256,
                "ownership_epoch": owned.record["epoch"],
                "status": "active",
            }
            observation = callbacks.observe(expected)
            if observation.classification != "intended_effect_present":
                raise StateError("ACTIVE_POINTER_MISMATCH")
            if mapping["status"] != "active":
                with exclusive_lock(
                    requests_root / ".lock", root=root, hook=crash_hook
                ):
                    current_mapping = _read_request_record(mapping_path, requests_root)
                    if current_mapping["start_input_v1_sha256"] != start_sha:
                        raise StateError("REQUEST_MAPPING_CONFLICT")
                    _write_request_record(
                        mapping_path,
                        requests_root,
                        {**current_mapping, "status": "active"},
                        crash_hook,
                    )
            return BootstrapResult(
                "active", run.name, run, owned.record["epoch"], first.generation
            )
        pointer = {
            "schema_version": "beads.run-pointer.v1",
            "run_id": run.name,
            "checkpoint_generation": first.generation,
            "checkpoint_sha256": first.generation_sha256,
            "ownership_epoch": owned.record["epoch"],
            "status": "active",
        }
        pointer_raw = canonical_payload_bytes(pointer)
        pointer_path = run / "bootstrap-pointer-input.json"
        atomic_write(
            pointer_path, pointer_raw + b"\n", root=run, max_bytes=512, hook=crash_hook
        )
        pointer_sha = sha256_bytes(pointer_raw)
        before = callbacks.observe(pointer)
        pointer_operation = semantic_operation_id(
            {
                "schema": "beads.run-pointer.v1",
                "effect_type": "ACTIVE_POINTER_PUBLISH",
                "target_identity": request.root_issue_id,
                "immutable_input_sha256": pointer_sha,
                "ownership_epoch": owned.record["epoch"],
            }
        )
        prepared = next(
            (
                item
                for item in journal.read().records
                if item["phase"] == "PREPARED"
                and item["operation_id"] == pointer_operation
            ),
            None,
        ) or _prepared_event(
            run=run,
            operation_id=pointer_operation,
            effect_type="ACTIVE_POINTER_PUBLISH",
            input_path=pointer_path,
            input_sha=pointer_sha,
            expected_sha=before.state_sha256,
            probe=_bootstrap_probe(
                request.root_issue_id, before.state_sha256, pointer_sha, "tracker_state"
            ),
            timestamp=now,
            issue_id=request.root_issue_id,
            epoch=owned.record["epoch"],
        )
        journal.append(prepared, hook=crash_hook)
        outcome = (
            before
            if before.classification == "intended_effect_present"
            else callbacks.publish(pointer)
        )
        crash_hook("after_pointer_publication")
        classification_status = {
            "intended_effect_present": "APPLIED",
            "prestate_unchanged": "NOT_APPLIED",
            "conflicting_effect": "CONFLICT",
            "insufficient_observation": "UNKNOWN",
        }
        if outcome.classification not in classification_status:
            raise StateError("POINTER_OBSERVATION_INVALID")
        evidence_value = {
            "classification": outcome.classification,
            "state_sha256": outcome.state_sha256,
        }
        evidence_path = run / "bootstrap-pointer-evidence.json"
        evidence_raw = canonical_bytes(evidence_value)
        atomic_write(
            evidence_path,
            evidence_raw,
            root=run,
            max_bytes=MAX_MANIFEST_BYTES,
            hook=crash_hook,
        )
        evidence_sha = sha256_bytes(evidence_raw)
        resolution_status = classification_status[outcome.classification]
        journal.append(
            _resolution_event(
                prepared,
                status=resolution_status,
                observed_sha=outcome.state_sha256,
                evidence_path=evidence_path,
                evidence_sha=evidence_sha,
                timestamp=now,
            ),
            hook=crash_hook,
        )
        if resolution_status != "APPLIED":
            terminal = (
                "conflict" if resolution_status == "CONFLICT" else "recovery_required"
            )
            with exclusive_lock(requests_root / ".lock", root=root, hook=crash_hook):
                mapping = {**mapping, "status": terminal}
                _write_request_record(mapping_path, requests_root, mapping, crash_hook)
            return BootstrapResult(
                terminal, run.name, run, owned.record["epoch"], first.generation
            )
        second_value = _empty_bootstrap_checkpoint(
            run,
            manifest,
            2,
            first.generation_sha256,
            owned.record,
            now,
            request.authority_snapshot_sha256,
        )
        second = store.accept(second_value, hook=crash_hook)
        with exclusive_lock(requests_root / ".lock", root=root, hook=crash_hook):
            current_mapping = _read_request_record(mapping_path, requests_root)
            if (
                current_mapping["start_input_v1_sha256"] != start_sha
                or current_mapping["run_id"] != run.name
            ):
                raise StateError("REQUEST_MAPPING_CONFLICT")
            _write_request_record(
                mapping_path,
                requests_root,
                {**current_mapping, "status": "active"},
                crash_hook,
            )
        return BootstrapResult(
            "active", run.name, run, owned.record["epoch"], second.generation
        )
