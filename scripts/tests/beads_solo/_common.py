"""Shared harness for the ``beads_solo`` fixture matrix (scc-0pu.15 / t14).

Deliberately self-contained rather than importing ``beads_tracker/_common.py``:
that module lives outside this suite's owned paths and is a frozen sibling
artifact, not a shared library. The loader/``FakeNative``/crash-hook shapes
below mirror it because they encode the same tested contract (dataclass field
resolution under ``from __future__ import annotations`` needs ``sys.modules``
registered before ``exec_module``; ``safe_bd.run_profile`` shells out to a
real pinned ``bd`` binary, so tests fake at the transport boundary instead).
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, cast

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "skills/beads/scripts"

BASE_MODULES = (
    "safe_output",
    "schema_runtime",
    "coordinator_state",
    "beads_ownership",
    "safe_bd",
    "operation_result",
)

_counter = {"n": 0}


def load(path):
    _counter["n"] += 1
    spec = importlib.util.spec_from_file_location(
        f"beads_solo_{_counter['n']}_{path.stem}", path
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def modules(*extra):
    sys.path.insert(0, str(SCRIPTS))

    class Bundle:
        pass

    bundle = Bundle()
    for name in BASE_MODULES + tuple(extra):
        setattr(bundle, name, load(SCRIPTS / f"{name}.py"))
    return bundle


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def make_run_id(seed=0):
    prefix = hashlib.sha256(f"beads-solo-{seed}".encode()).hexdigest()[:16]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%f") + "Z"
    suffix = base64.b32encode(secrets.token_bytes(5)).decode("ascii").rstrip("=")
    return f"run-{prefix}-{stamp}-{suffix}"


def digest(value, state):
    return state.sha256_bytes(state.canonical_bytes(value))


def make_run(
    m, repo, *, run_id=None, issue_ids=(), root_issue_id="scc-root", actor="solo"
):
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
    epochs = {}
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


def journal_records(run, **match):
    records = run["journal"].read().records
    return [r for r in records if all(r.get(k) == v for k, v in match.items())]


class NativeDenied(AssertionError):
    """A profile the test did not allow was dispatched to the tracker."""


class FakeNative:
    """Scripted ``safe_bd`` stand-in — see ``beads_tracker/_common.py`` for the
    identical contract this mirrors: raise rather than silently succeed on any
    profile/queue-exhaustion the test did not explicitly script.
    """

    def __init__(self, safe_bd, *, responses=None, allowed=None, workspace_sha256=None):
        self.safe_bd = safe_bd
        self.responses = {
            k: list(v) if isinstance(v, list) else [v]
            for k, v in (responses or {}).items()
        }
        self.allowed = frozenset(allowed) if allowed is not None else None
        self.workspace_sha256 = workspace_sha256 or ("a" * 64)
        self.calls = []

    @property
    def profiles(self):
        return [call.profile for call in self.calls]

    def count(self, profile):
        return self.profiles.count(profile)

    def ok(self, profile, data):
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

    def error(self, profile, code="BD_NATIVE_ERROR"):
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

    def __call__(self, request, *, sensitive=None):
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


def comments(*bodies):
    """Script one ``issue_comments`` reply carrying the given comment bodies.

    ``FakeNative`` reads a list value as a queue of successive replies, so a
    reply that is itself a list must be double-wrapped.
    """
    return [[{"text": body} for body in bodies]]


class CrashHook:
    """A ``crash_hook``-shaped callable that raises once its label fires.

    Exposes the per-instance exception type as a plain attribute
    (``crash.Crash``) so a test can ``pytest.raises(crash.Crash)`` without the
    dynamic-attribute-on-a-function-object pattern (``hook.Crash = Crash``)
    that is invisible to static type checkers.
    """

    def __init__(self, label):
        self.label = label

        class Crash(RuntimeError):
            pass

        self.Crash = Crash

    def __call__(self, event):
        if event == self.label:
            raise self.Crash(self.label)


def crash_after(label):
    """Raise the moment ``crash_hook`` observes ``label``, exactly once."""
    return CrashHook(label)


class NthCrashHook:
    """Like :class:`CrashHook`, but raises only on the ``n``-th (1-indexed)
    occurrence of ``label``.

    Needed wherever a single call fires the same crash-hook label more than
    once — e.g. ``coordinator_tracker.start_run`` fires
    ``after_pointer_publication`` twice per bootstrap (checkpoint 1's own
    accept, then the root run pointer itself); a first-match hook would always
    hit the first occurrence and could never exercise the second.
    """

    def __init__(self, label, n):
        self.label = label
        self.n = n
        self.seen = 0

        class Crash(RuntimeError):
            pass

        self.Crash = Crash

    def __call__(self, event):
        if event == self.label:
            self.seen += 1
            if self.seen == self.n:
                raise self.Crash(f"{self.label}#{self.n}")


def crash_on_nth(label, n):
    """Raise only on the ``n``-th (1-indexed) occurrence of ``label``."""
    return NthCrashHook(label, n)


class FakeRunner:
    """Scripted stand-in for the ``safe_output.CommandSpec`` runner surface
    used by ``protected_action.resolve_protected_action``/``coordinator_handoff.accept``.

    ``responses`` maps an opaque call index (0-based, in call order) to either
    a ``(result, observed_text)`` tuple to return or an exception instance/
    factory to raise (simulating a probe that could not be dispatched at all).
    """

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
        self.count = 0

    def __call__(self, spec, *, sensitive=None, callback=None):
        self.calls.append(spec)
        reply = (
            self.replies[self.count]
            if self.count < len(self.replies)
            else self.replies[-1]
        )
        self.count += 1
        if isinstance(reply, BaseException):
            raise reply
        if callable(reply) and not isinstance(reply, tuple):
            reply = cast(Callable[[Any], Any], reply)(spec)
        return reply
