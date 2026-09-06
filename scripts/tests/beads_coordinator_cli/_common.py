"""Shared fixtures for the coordinator CLI (``beads_coordinator.py``) suite.

Unlike the sibling suites under ``beads_tracker/`` and ``beads_front/``,
which load each frozen module under a uniquely-suffixed ``sys.modules`` name
so unrelated suites cannot collide, this suite loads ``beads_coordinator``
itself under its own real module name (a plain ``import`` once the scripts
directory is on ``sys.path``, exactly like ``test_beads_reconcile_run.py``
already does for ``coordinator_state``).  That matters here specifically:
``beads_coordinator.py`` does ``sys.path.insert(...)`` then plain
``import coordinator_tracker`` / ``import beads_ownership`` / etc, and none
of its CLI handlers expose a ``runner=`` injection point.  Loading it under
its real name means every frozen module it (transitively) imports --
including ``safe_bd``, reached through ``direct_operation``,
``coordinator_tracker`` and ``capture_beads_snapshot`` -- resolves to one
canonical module object shared by this whole process.  A single
``monkeypatch.setattr(safe_bd, "run_profile", fake)`` therefore intercepts
every native call the CLI makes, however many frozen-module hops away.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence, cast

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "skills" / "beads" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import beads_coordinator as bc  # noqa: E402
import beads_ownership  # noqa: E402
import coordinator_state as state  # noqa: E402
import safe_bd  # noqa: E402

CLI = SCRIPTS / "beads_coordinator.py"


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def make_run_id(seed: int = 0) -> str:
    """Build a run id matching the schema's exact run-id grammar."""
    prefix = hashlib.sha256(f"beads-coordinator-cli-{seed}".encode()).hexdigest()[:16]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%f") + "Z"
    suffix = base64.b32encode(secrets.token_bytes(5)).decode("ascii").rstrip("=")
    return f"run-{prefix}-{stamp}-{suffix}"


def make_run(
    repo: Path,
    *,
    run_id: str | None = None,
    issue_ids: Sequence[str] = (),
    root_issue_id: str = "scc-root",
    actor: str = "parent",
    created_at: str | None = None,
) -> dict[str, Any]:
    """Bootstrap a run directory, journal, checkpoint store, and ownership.

    Mirrors ``beads_tracker/_common.py``'s fixture of the same name, bound
    against the real, shared frozen-module objects ``beads_coordinator``
    itself imports (``state``, ``beads_ownership``) rather than a
    uniquely-named bundle copy.

    ``ownership`` is deliberately rooted at ``run_root`` (the *parent* of
    ``run_directory``), matching ``beads_ownership.OwnershipStore``'s own
    contract (a run directory must be a direct child of the store's root)
    and ``reconcile_run.status``'s own usage of ``run.parent``.
    """
    run_id = run_id or make_run_id()
    hermes = repo / ".hermes"
    hermes.mkdir(mode=0o700, exist_ok=True)
    run_root = hermes / "beads-runs"
    run_root.mkdir(mode=0o700, exist_ok=True)
    for path in (hermes, run_root):
        path.chmod(0o700)
    run_directory = run_root / run_id
    state.ensure_owner_directory(run_directory, root=run_root)
    manifest = state.make_run_manifest(
        run_id=run_id,
        request_id="request-0001-aaaaaaaaaaaa",
        repository_root=str(repo),
        workspace=str(repo),
        workspace_identity_sha256=hashlib.sha256(str(repo).encode()).hexdigest(),
        root_issue_id=root_issue_id,
        created_at=created_at or now(),
        coordinator_version="beads-coordinator-v1",
        run_root=str(run_root),
        run_secret_hex=secrets.token_bytes(32).hex(),
    )
    state.publish_run_manifest(run_directory, manifest)
    journal = state.OperationJournal.create(run_directory)
    checkpoints = state.CheckpointStore(run_directory, journal)
    ownership = beads_ownership.OwnershipStore(run_root)
    epochs: dict[str, int] = {}
    for issue_id in issue_ids:
        record = ownership.acquire(
            issue_id=issue_id,
            actor=actor,
            run_directory=run_directory,
            tracker_state_sha256="0" * 64,
            operation_id="0" * 64,
            now=datetime.now(timezone.utc),
        )
        assert record.disposition == "held", record.disposition
        assert record.record is not None
        epochs[issue_id] = record.record["epoch"]
    return {
        "run_id": run_id,
        "repo": repo,
        "run_root": run_root,
        "run_directory": run_directory,
        "manifest": manifest,
        "journal": journal,
        "checkpoints": checkpoints,
        "ownership": ownership,
        "epochs": epochs,
        "actor": actor,
        "root_issue_id": root_issue_id,
    }


def journal_records(run: Mapping[str, Any], **match: Any) -> list[dict[str, Any]]:
    """Read the operation journal, keeping only records matching every field."""
    records = run["journal"].read().records
    return [r for r in records if all(r.get(k) == v for k, v in match.items())]


class NativeDenied(AssertionError):
    """A profile the test did not allow was dispatched to the tracker."""


class FakeNative:
    """Scripted ``safe_bd.run_profile`` stand-in with a command-denial spy.

    Identical contract to ``beads_tracker/_common.py::FakeNative`` -- kept
    as a separate copy rather than an import so this suite has no coupling
    to a sibling suite's internal module (which is owned by a different
    concurrent workstream on this issue set).
    """

    def __init__(
        self,
        safe_bd_module,
        *,
        responses: Mapping[str, Any] | None = None,
        allowed: Sequence[str] | None = None,
        workspace_sha256: str | None = None,
    ) -> None:
        self.safe_bd = safe_bd_module
        self.responses = {
            k: list(v) if isinstance(v, list) else [v]
            for k, v in (responses or {}).items()
        }
        self.allowed = frozenset(allowed) if allowed is not None else None
        self.workspace_sha256 = workspace_sha256 or ("a" * 64)
        self.calls: list[Any] = []

    @property
    def profiles(self) -> list[str]:
        return [call.profile for call in self.calls]

    def count(self, profile: str) -> int:
        return self.profiles.count(profile)

    def ok(self, profile: str, data: Any) -> Any:
        return self.safe_bd.SafeBdResult(
            "beads.safe-bd-result.v1",
            profile,
            "ok",
            self.safe_bd.PINNED_BD_VERSION,
            self.workspace_sha256,
            data,
            (),
            None,
        )

    def error(self, profile: str, code: str = "BD_NATIVE_ERROR") -> Any:
        return self.safe_bd.SafeBdResult(
            "beads.safe-bd-result.v1",
            profile,
            "native_error",
            self.safe_bd.PINNED_BD_VERSION,
            None,
            None,
            (),
            code,
        )

    def __call__(self, request, *, sensitive: Any = None) -> Any:
        self.calls.append(request)
        profile = request.profile
        if self.allowed is not None and profile not in self.allowed:
            raise NativeDenied(profile)
        queue = self.responses.get(profile)
        if queue is None:
            raise NativeDenied(f"unscripted:{profile}")
        if not queue:
            raise NativeDenied(f"empty-reply-queue:{profile}")
        reply = queue.pop(0) if len(queue) > 1 else queue[0]
        if callable(reply):
            reply = cast(Callable[[Any], Any], reply)(request)
        if isinstance(reply, self.safe_bd.SafeBdResult):
            return reply
        return self.ok(profile, reply)


class CrashHook:
    """A ``crash_hook``-shaped callable that raises once its label fires.

    Structurally satisfies every frozen module's ``crash_hook: Callable[[str],
    None]`` parameter (``coordinator_state``, ``beads_ownership``, ...), while
    also exposing the per-instance exception type as a plain attribute
    (``crash.Crash``) so a test can ``pytest.raises(crash.Crash)`` without the
    dynamic-attribute-on-a-function-object pattern the sibling suites use
    (``hook.Crash = Crash``) -- that pattern is invisible to ``ty``, which has
    no way to know a plain ``def hook(...):`` object ever gains a ``Crash``
    attribute after the fact.
    """

    def __init__(self, label: str) -> None:
        self.label = label

        class Crash(RuntimeError):
            pass

        self.Crash = Crash

    def __call__(self, event: str) -> None:
        if event == self.label:
            raise self.Crash(self.label)


def crash_after(label: str) -> CrashHook:
    """A crash hook that raises the moment the named injection point fires."""
    return CrashHook(label)


def tracker_double(root_issue_id: str) -> tuple["FakeNative", dict[str, Any]]:
    """A tiny in-memory tracker double for root-pointer publish round trips.

    Round-trips whatever ``set_run_pointer`` writes back out of the next
    ``issue_get``, without hand-computing the exact canonical JSON in
    advance. Ported from ``beads_tracker/test_coordinator_tracker.py``'s
    fixture of the same name, rebound against ``bc.coordinator_tracker``'s
    own private metadata key so it works against the real, shared module
    object this suite loads ``beads_coordinator`` under. Needed by both
    ``start-run`` (``coordinator_tracker.start_run`` -> ``bootstrap_run``
    publishes the root pointer once) and ``finish`` (``pointer_callbacks``
    observes, then conditionally publishes, the terminal pointer) -- neither
    CLI handler exposes a ``runner=`` injection point, so both dispatch
    through the real, shared ``safe_bd.run_profile`` a test must monkeypatch.
    """
    box: dict[str, dict[str, str]] = {"metadata": {}}

    def get_issue(_request: Any) -> dict[str, Any]:
        return {"id": root_issue_id, "metadata": dict(box["metadata"])}

    def set_pointer(request: Any) -> dict[str, Any]:
        key = bc.coordinator_tracker._POINTER_METADATA_KEY
        box["metadata"][key] = request.arguments["value"]
        return {"id": root_issue_id, "metadata": dict(box["metadata"])}

    fake = FakeNative(
        safe_bd,
        responses={"issue_get": get_issue, "set_run_pointer": set_pointer},
        allowed=["issue_get", "set_run_pointer"],
    )
    return fake, box


def namespace(**kwargs: Any):
    """Build a bare ``argparse.Namespace``-shaped object for handler tests.

    The CLI handlers only ever read attributes off ``args`` -- they never
    call an argparse-specific method -- so a plain namespace is enough to
    drive ``bc._handle_*`` directly without going through ``bc.main`` and a
    real argv/stdout round trip.
    """
    import argparse

    return argparse.Namespace(**kwargs)
