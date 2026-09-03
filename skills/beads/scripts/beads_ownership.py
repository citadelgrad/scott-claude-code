#!/usr/bin/env python3
"""Cooperative same-filesystem ownership epochs for Beads coordinator runs."""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import coordinator_state as state
import schema_runtime

LEASE_SECONDS = 15 * 60
HASH_RE = re.compile(r"^[0-9a-f]{64}$")


class OwnershipError(state.StateError):
    """Typed ownership invariant failure."""


@dataclass(frozen=True)
class OwnershipResult:
    disposition: str
    record: dict[str, Any] | None
    history: tuple[dict[str, Any], ...]
    limitation: str = "cooperative_local_filesystem_only"


def derive_token(run_secret: bytes, issue_id: str, epoch: int) -> bytes:
    """Derive a raw capability for in-process comparison only."""
    if len(run_secret) != 32 or not 0 <= epoch <= state.MAX_U64:
        raise OwnershipError("OWNERSHIP_TOKEN_INPUT_INVALID")
    issue = issue_id.encode("utf-8", "strict")
    return hmac.new(
        run_secret,
        b"beads-owner-v1\x00" + issue + b"\x00" + epoch.to_bytes(8, "big"),
        hashlib.sha256,
    ).digest()


def _parse_time(value: str) -> dt.datetime:
    try:
        parsed = dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError as exc:
        raise OwnershipError("OWNERSHIP_TIMESTAMP_INVALID") from exc
    return parsed.replace(tzinfo=dt.timezone.utc)


def _now(value: dt.datetime | None) -> dt.datetime:
    current = value or dt.datetime.now(dt.timezone.utc)
    if current.tzinfo is None:
        raise OwnershipError("OWNERSHIP_TIMESTAMP_INVALID")
    return current.astimezone(dt.timezone.utc)


def _valid_hash(value: str, code: str) -> None:
    if not isinstance(value, str) or not HASH_RE.fullmatch(value):
        raise OwnershipError(code)


class OwnershipStore:
    """Per-issue lock/history/current storage beneath one approved run root."""

    def __init__(
        self, run_root: Path, *, crash_hook: Callable[[str], None] = state.NOOP_HOOK
    ) -> None:
        self.run_root = state.validate_owner_directory(Path(run_root))
        self.root = self.run_root / "_ownership"
        state.ensure_owner_directory(self.root, root=self.run_root)
        self.crash_hook = crash_hook

    def _directory(self, issue_id: str) -> Path:
        key = state.issue_key(issue_id)
        directory = self.root / key
        state.ensure_owner_directory(directory, root=self.run_root)
        return directory

    def _run(self, run_directory: Path) -> tuple[dict[str, Any], bytes]:
        try:
            relative = run_directory.absolute().relative_to(self.run_root.absolute())
        except ValueError as exc:
            raise OwnershipError("RUN_OUTSIDE_OWNERSHIP_ROOT") from exc
        if len(relative.parts) != 1 or relative.name.startswith("_"):
            raise OwnershipError("RUN_OUTSIDE_OWNERSHIP_ROOT")
        guarded = state.guarded_path(self.run_root, relative, must_exist=True)
        manifest = state.load_run_manifest(guarded)
        if manifest["run_root"] != str(self.run_root.resolve()):
            raise OwnershipError("RUN_ROOT_IDENTITY_MISMATCH")
        try:
            secret = bytes.fromhex(manifest["run_secret_hex"])
        except ValueError as exc:
            raise OwnershipError("RUN_SECRET_INVALID") from exc
        if len(secret) != 32:
            raise OwnershipError("RUN_SECRET_INVALID")
        return manifest, secret

    def _read_unlocked(
        self, directory: Path, issue_id: str
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        history_path = directory / "events.jsonl"
        current_path = directory / "current.json"
        history_exists = history_path.exists()
        current_exists = current_path.exists()
        if history_exists != current_exists:
            code = (
                "OWNERSHIP_HISTORY_MISSING"
                if current_exists
                else "OWNERSHIP_CURRENT_MISSING"
            )
            raise OwnershipError(code)
        if not history_exists:
            return [], None
        try:
            history_raw = state.validate_owner_file(
                history_path, root=self.run_root
            ).read_bytes()
            parsed = state._read_canonical_lines(
                history_raw, "ownership-history-event-v1.schema.json"
            )
        except state.StateError as exc:
            raise OwnershipError("OWNERSHIP_HISTORY_INVALID") from exc
        if parsed.torn_tail is not None:
            raise OwnershipError("OWNERSHIP_HISTORY_INVALID")
        if not parsed.records:
            raise OwnershipError("OWNERSHIP_HISTORY_MISSING")
        history = list(parsed.records)
        previous_epoch = 0
        for event in history:
            if event["issue_id"] != issue_id:
                raise OwnershipError("OWNERSHIP_ISSUE_MISMATCH")
            epoch = event["epoch"]
            if epoch < previous_epoch or epoch > previous_epoch + 1:
                raise OwnershipError("OWNERSHIP_EPOCH_SEQUENCE_INVALID")
            if epoch == previous_epoch + 1 and event["status"] != "active":
                raise OwnershipError("OWNERSHIP_EPOCH_SEQUENCE_INVALID")
            previous_epoch = epoch
        current_raw = state.validate_owner_file(
            current_path, root=self.run_root
        ).read_bytes()
        try:
            current = schema_runtime.strict_json_loads(
                current_raw, max_bytes=state.MAX_MANIFEST_BYTES
            )
            schema_runtime.require_valid("ownership-record-v1.schema.json", current)
        except Exception as exc:
            raise OwnershipError("OWNERSHIP_CURRENT_INVALID") from exc
        if state.canonical_bytes(current) != current_raw:
            raise OwnershipError("OWNERSHIP_CURRENT_INVALID")
        last = history[-1]
        matching = (
            current["issue_id"] == issue_id
            and current["issue_key"] == directory.name
            and current["run_id"] == last["run_id"]
            and current["epoch"] == last["epoch"]
            and current["status"] == last["status"]
            and current["token_sha256"] == last["token_sha256"]
        )
        if not matching:
            raise OwnershipError("OWNERSHIP_CURRENT_HISTORY_MISMATCH")
        return history, current

    def _append_event(
        self,
        directory: Path,
        history: list[dict[str, Any]],
        record: dict[str, Any],
        operation_id: str,
        timestamp: str,
    ) -> dict[str, Any]:
        _valid_hash(operation_id, "OWNERSHIP_OPERATION_ID_INVALID")
        history_path = directory / "events.jsonl"
        if not history_path.exists():
            state.create_owner_file(history_path, root=self.run_root)
        previous = (
            state.sha256_bytes(state.canonical_bytes(history[-1]))
            if history
            else state.GENESIS_SHA256
        )
        event = {
            "schema_version": "beads.ownership-history-event.v1",
            "issue_id": record["issue_id"],
            "run_id": record["run_id"],
            "epoch": record["epoch"],
            "status": record["status"],
            "token_sha256": record["token_sha256"],
            "previous_event_sha256": previous,
            "operation_id": operation_id,
            "timestamp": timestamp,
        }
        schema_runtime.require_valid("ownership-history-event-v1.schema.json", event)
        fd = os.open(
            history_path,
            os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            os.write(fd, state.canonical_bytes(event))
            self.crash_hook("after_ownership_history_write")
            os.fsync(fd)
            self.crash_hook("after_ownership_history_fsync")
        finally:
            os.close(fd)
        history.append(event)
        return event

    def _publish_current(self, directory: Path, record: dict[str, Any]) -> None:
        schema_runtime.require_valid("ownership-record-v1.schema.json", record)
        state.atomic_write(
            directory / "current.json",
            state.canonical_bytes(record),
            root=self.run_root,
            max_bytes=state.MAX_MANIFEST_BYTES,
            hook=self.crash_hook,
        )
        self.crash_hook("after_ownership_current_publication")

    def inspect(self, issue_id: str) -> OwnershipResult:
        directory = self._directory(issue_id)
        with state.exclusive_lock(
            directory / "lock", root=self.run_root, hook=self.crash_hook
        ):
            history, current = self._read_unlocked(directory, issue_id)
            if current is None:
                return OwnershipResult("unheld", None, tuple(history))
            disposition = {
                "active": "held",
                "released": "released",
                "release_prepared": "unknown",
                "unknown": "unknown",
                "conflict": "conflict",
            }[current["status"]]
            return OwnershipResult(disposition, current, tuple(history))

    def acquire(
        self,
        *,
        issue_id: str,
        actor: str,
        run_directory: Path,
        tracker_state_sha256: str,
        operation_id: str,
        now: dt.datetime | None = None,
    ) -> OwnershipResult:
        _valid_hash(tracker_state_sha256, "TRACKER_STATE_HASH_INVALID")
        _valid_hash(operation_id, "OWNERSHIP_OPERATION_ID_INVALID")
        state._opaque_utf8(actor, label="ACTOR", maximum=4096)
        manifest, secret = self._run(run_directory)
        directory = self._directory(issue_id)
        current_time = _now(now)
        timestamp = state.utc_timestamp(current_time)
        with state.exclusive_lock(
            directory / "lock", root=self.run_root, hook=self.crash_hook
        ):
            history, current = self._read_unlocked(directory, issue_id)
            if current is not None and current["status"] != "released":
                if (
                    current["status"] == "active"
                    and current["run_id"] == manifest["run_id"]
                    and current["actor"] == actor
                    and current["acquisition_operation_id"] == operation_id
                ):
                    return OwnershipResult("held", current, tuple(history))
                if current["status"] == "active":
                    return OwnershipResult("conflict", current, tuple(history))
                raise OwnershipError("OWNERSHIP_NOT_ACTIVE", status="unknown")
            previous_epoch = current["epoch"] if current is not None else 0
            if previous_epoch >= state.MAX_U64:
                raise OwnershipError("OWNERSHIP_EPOCH_OVERFLOW")
            epoch = previous_epoch + 1
            token_hash = state.sha256_bytes(derive_token(secret, issue_id, epoch))
            record = {
                "schema_version": "beads.ownership-record.v1",
                "issue_id": issue_id,
                "issue_key": state.issue_key(issue_id),
                "actor": actor,
                "run_id": manifest["run_id"],
                "epoch": epoch,
                "token_sha256": token_hash,
                "acquisition_operation_id": operation_id,
                "tracker_state_sha256": tracker_state_sha256,
                "acquired_at": timestamp,
                "renewed_at": timestamp,
                "lease_expires_at": state.utc_timestamp(
                    current_time + dt.timedelta(seconds=LEASE_SECONDS)
                ),
                "status": "active",
            }
            self._append_event(directory, history, record, operation_id, timestamp)
            self._publish_current(directory, record)
            return OwnershipResult("held", record, tuple(history))

    def _require_capability(
        self,
        issue_id: str,
        run_directory: Path,
        epoch: int,
        history: list[dict[str, Any]],
        current: dict[str, Any] | None,
    ) -> tuple[dict[str, Any], bytes]:
        manifest, secret = self._run(run_directory)
        if current is None or current["status"] != "active":
            raise OwnershipError("OWNERSHIP_NOT_ACTIVE", status="unknown")
        if current["epoch"] != epoch:
            raise OwnershipError("OWNERSHIP_EPOCH_STALE")
        if current["run_id"] != manifest["run_id"]:
            raise OwnershipError("OWNERSHIP_RUN_MISMATCH")
        expected = state.sha256_bytes(derive_token(secret, issue_id, epoch))
        if not hmac.compare_digest(expected, current["token_sha256"]):
            raise OwnershipError("OWNERSHIP_TOKEN_MISMATCH")
        if not history:
            raise OwnershipError("OWNERSHIP_HISTORY_MISSING")
        return current, secret

    def renew(
        self,
        issue_id: str,
        run_directory: Path,
        *,
        epoch: int,
        operation_id: str,
        now: dt.datetime | None = None,
    ) -> OwnershipResult:
        _valid_hash(operation_id, "OWNERSHIP_OPERATION_ID_INVALID")
        directory = self._directory(issue_id)
        current_time = _now(now)
        timestamp = state.utc_timestamp(current_time)
        with state.exclusive_lock(
            directory / "lock", root=self.run_root, hook=self.crash_hook
        ):
            history, loaded = self._read_unlocked(directory, issue_id)
            current, _secret = self._require_capability(
                issue_id, run_directory, epoch, history, loaded
            )
            if current_time > _parse_time(current["lease_expires_at"]):
                unknown = {**current, "status": "unknown", "renewed_at": timestamp}
                self._append_event(directory, history, unknown, operation_id, timestamp)
                self._publish_current(directory, unknown)
                raise OwnershipError("OWNERSHIP_LEASE_EXPIRED", status="unknown")
            renewed = {
                **current,
                "renewed_at": timestamp,
                "lease_expires_at": state.utc_timestamp(
                    current_time + dt.timedelta(seconds=LEASE_SECONDS)
                ),
            }
            self._append_event(directory, history, renewed, operation_id, timestamp)
            self._publish_current(directory, renewed)
            return OwnershipResult("held", renewed, tuple(history))

    def verify(
        self,
        issue_id: str,
        run_directory: Path,
        *,
        epoch: int,
        now: dt.datetime | None = None,
    ) -> OwnershipResult:
        directory = self._directory(issue_id)
        current_time = _now(now)
        timestamp = state.utc_timestamp(current_time)
        with state.exclusive_lock(
            directory / "lock", root=self.run_root, hook=self.crash_hook
        ):
            history, loaded = self._read_unlocked(directory, issue_id)
            current, _secret = self._require_capability(
                issue_id, run_directory, epoch, history, loaded
            )
            if current_time > _parse_time(current["lease_expires_at"]):
                operation_id = state.semantic_operation_id(
                    {
                        "schema": "beads.ownership-expiry.v1",
                        "effect_type": "OWNERSHIP_EXPIRED",
                        "target_identity": issue_id,
                        "immutable_input_sha256": current["token_sha256"],
                        "ownership_epoch": epoch,
                    }
                )
                unknown = {**current, "status": "unknown", "renewed_at": timestamp}
                self._append_event(directory, history, unknown, operation_id, timestamp)
                self._publish_current(directory, unknown)
                return OwnershipResult("unknown", unknown, tuple(history))
            return OwnershipResult("held", current, tuple(history))

    def release(
        self,
        issue_id: str,
        run_directory: Path,
        *,
        epoch: int,
        operation_id: str,
        now: dt.datetime | None = None,
    ) -> OwnershipResult:
        _valid_hash(operation_id, "OWNERSHIP_OPERATION_ID_INVALID")
        directory = self._directory(issue_id)
        current_time = _now(now)
        timestamp = state.utc_timestamp(current_time)
        with state.exclusive_lock(
            directory / "lock", root=self.run_root, hook=self.crash_hook
        ):
            history, loaded = self._read_unlocked(directory, issue_id)
            current, _secret = self._require_capability(
                issue_id, run_directory, epoch, history, loaded
            )
            if current_time > _parse_time(current["lease_expires_at"]):
                raise OwnershipError("OWNERSHIP_LEASE_EXPIRED", status="unknown")
            prepared = {
                **current,
                "status": "release_prepared",
                "renewed_at": timestamp,
            }
            self._append_event(directory, history, prepared, operation_id, timestamp)
            self._publish_current(directory, prepared)
            released = {**prepared, "status": "released"}
            self._append_event(directory, history, released, operation_id, timestamp)
            self._publish_current(directory, released)
            return OwnershipResult("released", released, tuple(history))
