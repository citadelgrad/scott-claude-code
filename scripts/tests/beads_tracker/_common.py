"""Shared fixtures for the guarded tracker / direct-operation suites.

The four modules under test (``coordinator_tracker``, ``direct_operation``,
``protected_action``, ``coordinator_handoff``) are the only ones in the skill
that reach a real ``bd`` process.  ``safe_bd.run_profile`` refuses to run
without an installed, version-pinned, contract-hashed ``bd`` binary, so every
test here drives the runtime through :class:`FakeNative` instead: a scripted
runner that records each :class:`safe_bd.SafeBdRequest` it is handed and
denies every profile that was not explicitly allowed.  That makes "no child
tracker writes" and "no mutation attempted after a refusal" directly
assertable rather than merely intended.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence, cast

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "skills/beads/scripts"

# Every module the suites need before any of the four under test can import.
BASE_MODULES = (
    "safe_output",
    "schema_runtime",
    "coordinator_state",
    "beads_ownership",
    "safe_bd",
    "operation_result",
)

_counter = {"n": 0}


def load(path: Path):
    _counter["n"] += 1
    spec = importlib.util.spec_from_file_location(
        f"beads_tracker_{_counter['n']}_{path.stem}", path
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def modules(*extra: str):
    """Load the base modules plus the named modules under test.

    Each suite names only what it exercises, so a half-finished sibling module
    never breaks an unrelated suite at collection time.
    """
    sys.path.insert(0, str(SCRIPTS))

    class Bundle:
        pass

    bundle = Bundle()
    for name in BASE_MODULES + tuple(extra):
        setattr(bundle, name, load(SCRIPTS / f"{name}.py"))
    return bundle


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def make_run_id(seed: int = 0) -> str:
    """Build a run id matching the schema's exact run-id grammar."""
    prefix = hashlib.sha256(f"beads-tracker-{seed}".encode()).hexdigest()[:16]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%f") + "Z"
    suffix = base64.b32encode(secrets.token_bytes(5)).decode("ascii").rstrip("=")
    return f"run-{prefix}-{stamp}-{suffix}"


def digest(value: Any, state) -> str:
    return state.sha256_bytes(state.canonical_bytes(value))


def make_run(
    m,
    repo: Path,
    *,
    run_id: str | None = None,
    issue_ids: Sequence[str] = (),
    root_issue_id: str = "scc-root",
    actor: str = "parent",
) -> dict[str, Any]:
    """Bootstrap a run directory, journal, checkpoint store, and ownership."""
    state = m.coordinator_state
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
        created_at=now(),
        coordinator_version="beads-coordinator-v1",
        run_root=str(run_root),
        run_secret_hex=secrets.token_bytes(32).hex(),
    )
    state.publish_run_manifest(run_directory, manifest)
    journal = state.OperationJournal.create(run_directory)
    checkpoints = state.CheckpointStore(run_directory, journal)
    ownership = m.beads_ownership.OwnershipStore(run_root)
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
    """Read the operation journal, keeping only records matching every field.

    Whole-file substring assertions are wrong here: the journal legitimately
    carries ``"status": "APPLIED"`` from unrelated successful effects, so a
    caller must always scope by ``effect_type`` and ``phase``.
    """
    records = run["journal"].read().records
    return [r for r in records if all(r.get(k) == v for k, v in match.items())]


class NativeDenied(AssertionError):
    """A profile the test did not allow was dispatched to the tracker."""


class FakeNative:
    """Scripted ``safe_bd.run_profile`` stand-in with a command-denial spy.

    ``responses`` maps a profile name to either one scripted reply or a list of
    replies consumed in order.  A reply is a ``safe_bd.SafeBdResult``, a plain
    value (wrapped as a successful result), or a callable taking the request.
    Any profile absent from ``allowed`` raises :class:`NativeDenied`, which is
    how a suite proves an operation never attempted a write it must not make.
    """

    def __init__(
        self,
        safe_bd,
        *,
        responses: Mapping[str, Any] | None = None,
        allowed: Sequence[str] | None = None,
        workspace_sha256: str | None = None,
    ) -> None:
        self.safe_bd = safe_bd
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
            # A list value is always a queue of replies, never one list-shaped
            # reply.  ``{"issue_comments": []}`` therefore scripts nothing at
            # all; a single empty-list reply is ``{"issue_comments": [[]]}``.
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
    None]`` parameter, while also exposing the per-instance exception type as a
    plain attribute (``crash.Crash``) so a test can ``pytest.raises(crash.Crash)``
    without the dynamic-attribute-on-a-function-object pattern
    (``hook.Crash = Crash``) that is invisible to static type checkers.
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
