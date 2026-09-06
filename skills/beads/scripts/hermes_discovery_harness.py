#!/usr/bin/env python3
"""Run hash-bound Hermes skill routing checks in throwaway profile homes.

The no-model ``probe`` exercises the frozen Hermes scanner and ``skill_view``
handler. The ``run`` command is intentionally authorization-gated because real
automatic routing is an LLM decision. It accepts private prompts over stdin,
retains only hashes and event counts, and removes every per-scenario home.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import stat
import subprocess
import sys
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

FROZEN_HERMES_COMMIT = "21b2095d00a98b8ad7b5c60b10587619c852cdb8"
FROZEN_DESIGN_SHA256 = (
    "29963dcb172bd3d7030c45eda797959a5341b9c0067c168c7340e1f2dc454734"
)
FROZEN_SPLIT_HASHES = {
    "public": "03be9aafd2e39605e7a31d82bedff9d5d24061d7b2dddb1986fa78fa26b975c6",
    "hidden": "394769a9ad916869c71732a7ce5039ad983059dbcbe560e5c9e327f8a3c29c30",
    "sealed": "9954b44ebc3bb95478abdf0a1f9f2f33ea9a842d6f2d6fdb9e3afc7920f4c864",
}
CORPUS_ENVELOPE_SCHEMA = "hermes-beads-routing-prompts.v1"
REPORT_SCHEMA = "hermes-beads-actual-discovery-report.v2"
SCENARIO_COUNT = 72
MAX_TURNS = 2
RUN_BUDGET_SECONDS = 180
# Bounded raw-output retention for errored lanes (Defect 3). A cap per stream
# stops a runaway process from filling the disk; a cap on how many errored
# lanes retain output at all bounds total disk use across a full sweep.
RAW_RETENTION_MAX_BYTES = 65_536
RAW_RETENTION_LANE_LIMIT = 3
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_VOLATILE_PARTS = {"__pycache__", ".pytest_cache", ".DS_Store"}
_CREDENTIAL_FILES = (".env", "auth.json")


class HarnessError(RuntimeError):
    """A fail-closed harness contract violation."""


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    split: str
    polarity: str
    prompt: str
    expected_load: bool


@dataclass(frozen=True)
class ScenarioResult:
    scenario_id: str
    split: str
    expected_load: bool
    discovery_events: int
    load_events: int
    exit_code: int
    stdout_sha256: str
    stderr_sha256: str
    state_sha256: str
    raw_stdout_path: str | None = None
    raw_stderr_path: str | None = None
    # Per-lane default-profile mutation attribution (scc-l41). Populated by
    # run_corpus from a before/after _profile_fingerprint pair taken around
    # this exact lane; paired with the sibling scenario_id field above, each
    # result row names both which lane ran and which tracked root(s), if
    # any, changed under it. Empty for a lane where nothing changed, and
    # empty by default here because _run_scenario (which builds this
    # dataclass) does not itself see the default-profile home -- only
    # run_corpus does, so it attaches this afterward via dataclasses.replace.
    profile_delta: list[str] = field(default_factory=list)

    @property
    def errored(self) -> bool:
        """True when the session did not complete cleanly.

        When this is true, routing is UNKNOWN, not wrong: the session may
        have crashed before it ever reached skill routing (for example, a
        rejected model call). Do not read an errored lane as a routing
        failure.
        """
        return self.exit_code != 0

    @property
    def _observed_load(self) -> bool:
        return self.discovery_events > 0 and self.load_events > 0

    @property
    def routed_correctly(self) -> bool:
        """Whether the observed load matched the expected load.

        Meaningful only when ``errored`` is false. On an errored lane the
        observed counts are typically zero as a side effect of the crash,
        not evidence the skill decided correctly, so this value must not be
        used for pass/fail purposes while errored.
        """
        return self._observed_load == self.expected_load

    @property
    def outcome(self) -> str:
        """One of 'errored', 'passed', 'failed' -- a strict three-way partition.

        An errored session is never folded into 'failed': a systemic outage
        (every lane erroring) must read as "N errored", not "N routing
        failures".
        """
        if self.errored:
            return "errored"
        return "passed" if self.routed_correctly else "failed"

    @property
    def passed(self) -> bool:
        return self.outcome == "passed"


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _iter_tree_files(root: Path) -> Iterable[Path]:
    root = Path(root)
    if not root.is_dir():
        raise HarnessError("TREE_ROOT_INVALID")
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root)
        if (
            any(part in _VOLATILE_PARTS for part in relative.parts)
            or path.suffix == ".pyc"
        ):
            continue
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise HarnessError("TREE_SYMLINK_FORBIDDEN")
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode):
            raise HarnessError("TREE_NONREGULAR_FORBIDDEN")
        yield path


def hash_tree(root: Path) -> str:
    """Hash relative paths, executable bits, lengths, and bytes deterministically."""
    root = Path(root)
    digest = hashlib.sha256()
    for path in _iter_tree_files(root):
        relative = path.relative_to(root).as_posix().encode()
        content = path.read_bytes()
        executable = b"1" if path.stat().st_mode & 0o111 else b"0"
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(executable)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _require_hash(value: str, code: str) -> str:
    if not isinstance(value, str) or not _HASH_RE.fullmatch(value):
        raise HarnessError(code)
    return value


def git_commit(repo: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        check=False,
        text=True,
        timeout=15,
    )
    commit = result.stdout.strip()
    if result.returncode or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise HarnessError("HERMES_SOURCE_IDENTITY_UNAVAILABLE")
    return commit


def verify_frozen_hermes(hermes_source: Path) -> None:
    if git_commit(hermes_source) != FROZEN_HERMES_COMMIT:
        raise HarnessError("HERMES_COMMIT_MISMATCH")


def _copy_tree_exact(source: Path, destination: Path) -> None:
    if destination.exists():
        raise HarnessError("INSTALL_TARGET_EXISTS")
    destination.mkdir(parents=True)
    for source_file in _iter_tree_files(source):
        relative = source_file.relative_to(source)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_file, target, follow_symlinks=False)
        os.chmod(target, stat.S_IMODE(source_file.stat().st_mode))


def install_candidate(candidate: Path, home: Path, expected_sha256: str) -> Path:
    expected = _require_hash(expected_sha256, "CANDIDATE_HASH_INVALID")
    candidate = Path(candidate).resolve()
    if hash_tree(candidate) != expected:
        raise HarnessError("CANDIDATE_HASH_MISMATCH")
    home = Path(home)
    home.mkdir(parents=True, exist_ok=False)
    os.chmod(home, 0o700)
    (home / ".no-bundled-skills").touch(mode=0o600)
    installed = home / "skills" / "beads"
    _copy_tree_exact(candidate, installed)
    if hash_tree(installed) != expected:
        raise HarnessError("INSTALLED_CANDIDATE_HASH_MISMATCH")
    return installed


def _load_json(path: Path, code: str) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise HarnessError(code) from error


def load_corpus(
    corpus_path: Path, design_path: Path, expected_sha256: str
) -> list[Scenario]:
    """Validate a custodian-provided private envelope without returning its text."""
    expected_sha = _require_hash(expected_sha256, "CORPUS_HASH_INVALID")
    if sha256_file(corpus_path) != expected_sha:
        raise HarnessError("CORPUS_HASH_MISMATCH")
    if sha256_file(design_path) != FROZEN_DESIGN_SHA256:
        raise HarnessError("DESIGN_HASH_MISMATCH")
    design = _load_json(design_path, "DESIGN_INVALID")
    envelope = _load_json(corpus_path, "CORPUS_INVALID")
    if not isinstance(envelope, dict):
        raise HarnessError("CORPUS_INVALID")
    if envelope.get("schema_version") != CORPUS_ENVELOPE_SCHEMA:
        raise HarnessError("CORPUS_SCHEMA_MISMATCH")
    if envelope.get("design_sha256") != FROZEN_DESIGN_SHA256:
        raise HarnessError("CORPUS_DESIGN_BINDING_MISMATCH")
    if envelope.get("split_hashes") != FROZEN_SPLIT_HASHES:
        raise HarnessError("CORPUS_SPLIT_BINDING_MISMATCH")

    expected_rows = {
        row[0]: {
            "split": row[1],
            "polarity": row[2],
            "expected_load": "RST" not in row[5],
        }
        for row in design.get("scenarios", [])
        if isinstance(row, list) and len(row) >= 6
    }
    raw_scenarios = envelope.get("scenarios")
    if len(expected_rows) != SCENARIO_COUNT or not isinstance(raw_scenarios, list):
        raise HarnessError("CORPUS_INDEX_MISMATCH")
    observed_ids = [
        row.get("scenario_id") for row in raw_scenarios if isinstance(row, dict)
    ]
    if len(raw_scenarios) != SCENARIO_COUNT or set(observed_ids) != set(expected_rows):
        raise HarnessError("CORPUS_INDEX_MISMATCH")
    if len(observed_ids) != len(set(observed_ids)):
        raise HarnessError("CORPUS_INDEX_MISMATCH")

    scenarios: list[Scenario] = []
    rows_by_id = {row["scenario_id"]: row for row in raw_scenarios}
    for scenario_id in sorted(expected_rows):
        row = rows_by_id[scenario_id]
        expected_row = expected_rows[scenario_id]
        prompt = row.get("prompt")
        if set(row) != {"scenario_id", "split", "polarity", "prompt"}:
            raise HarnessError("CORPUS_ROW_SHAPE_INVALID")
        if (
            row.get("split") != expected_row["split"]
            or row.get("polarity") != expected_row["polarity"]
        ):
            raise HarnessError("CORPUS_INDEX_MISMATCH")
        if not isinstance(prompt, str) or not prompt or len(prompt.encode()) > 65_536:
            raise HarnessError("CORPUS_PROMPT_INVALID")
        scenarios.append(
            Scenario(
                scenario_id=scenario_id,
                split=row["split"],
                polarity=row["polarity"],
                prompt=prompt,
                expected_load=bool(expected_row["expected_load"]),
            )
        )
    return scenarios


def approval_request(provider: str, model: str) -> dict[str, Any]:
    return {
        "hermes_sessions": SCENARIO_COUNT,
        "maximum_provider_requests": SCENARIO_COUNT * MAX_TURNS,
        "maximum_turns_per_session": MAX_TURNS,
        "run_budget_seconds_per_session": RUN_BUDGET_SECONDS,
        "provider": provider,
        "model": model,
        "billing_mode": "OpenAI Codex subscription quota"
        if provider == "openai-codex"
        else "provider-defined",
        "paid_api_fallback": False,
    }


# -- scc-ux6: real single-use authorization tokens --------------------------
#
# A prior defect let an agent compute a "valid" --authorization value
# directly from public source (a deterministic f-string over provider/model),
# so no real human action was ever required to launch a quota-consuming run.
# That incident is documented in beads issue scc-ux6.
#
# The fix: a local ledger of human-issued, unguessable, single-use tokens.
# A token must be minted out-of-band by a human (see
# ``issue_authorization_token`` / the ``issue-token`` CLI command) before it
# exists in the ledger at all -- an agent can never derive one from this
# file's source. Presenting a token consumes it atomically, immediately, and
# unconditionally: before any preflight check (e.g. CANDIDATE_HASH_MISMATCH)
# and before any subprocess is spawned. This means a second presentation of
# the same token -- regardless of whether the first attempt completed,
# failed a preflight check, or errored -- is always rejected, and an agent
# can never self-conclude that a prior, still-open authorization covers a
# new attempt.
DEFAULT_TOKEN_LEDGER_PATH = (
    Path.home() / ".hermes-discovery-harness" / "authorization-tokens.json"
)
TOKEN_BYTES = 32


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ledger_lock_path(ledger_path: Path) -> Path:
    return Path(str(ledger_path) + ".lock")


@contextlib.contextmanager
def _held_ledger_lock(ledger_path: Path):
    """Serialize read-modify-write access to the token ledger across processes.

    This locks a dedicated, never-replaced ``*.lock`` file rather than the
    ledger file itself. The ledger is rewritten via atomic replace-on-write
    (``_write_secure_bytes``), which swaps in a fresh inode on every write,
    so an ``flock`` held on the ledger file's own file descriptor would
    silently stop protecting anything the moment one writer replaced it.
    """
    ledger_path = Path(ledger_path)
    ledger_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = _ledger_lock_path(ledger_path)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _read_ledger(ledger_path: Path) -> list[dict[str, Any]]:
    ledger_path = Path(ledger_path)
    if not ledger_path.exists():
        return []
    try:
        entries = json.loads(ledger_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise HarnessError("AUTHORIZATION_LEDGER_INVALID") from error
    if not isinstance(entries, list):
        raise HarnessError("AUTHORIZATION_LEDGER_INVALID")
    return entries


def _write_ledger(ledger_path: Path, entries: list[dict[str, Any]]) -> None:
    rendered = json.dumps(entries, indent=2, sort_keys=True) + "\n"
    _write_secure_bytes(Path(ledger_path), rendered.encode("utf-8"))


def issue_authorization_token(
    ledger_path: Path = DEFAULT_TOKEN_LEDGER_PATH,
) -> str:
    """Mint one fresh, unguessable, single-use authorization token.

    This is the only supported way a token comes into existence. It must be
    invoked directly by a human -- for example via the ``issue-token`` CLI
    command -- never computed or inferred by an agent. The token is
    ``secrets.token_hex`` output (not derivable from this file's source) and
    is recorded, unconsumed, in the ledger before it is returned.
    """
    ledger_path = Path(ledger_path)
    token = secrets.token_hex(TOKEN_BYTES)
    with _held_ledger_lock(ledger_path):
        entries = _read_ledger(ledger_path)
        entries.append(
            {
                "token": token,
                "issued_at": _now_iso(),
                "consumed": False,
                "consumed_at": None,
            }
        )
        _write_ledger(ledger_path, entries)
    return token


def consume_authorization_token(
    token: str, ledger_path: Path = DEFAULT_TOKEN_LEDGER_PATH
) -> None:
    """Atomically look up and consume a single-use authorization token.

    Fail-closed, in order:
      * No ledger entry matches ``token`` at all -> AUTHORIZATION_TOKEN_MISSING.
      * The matching entry is already consumed -> AUTHORIZATION_TOKEN_ALREADY_CONSUMED.
      * Otherwise the entry is marked consumed here, immediately -- before
        the caller runs any preflight check or spawns any subprocess -- so a
        second presentation of this same token, from any later attempt for
        any reason, is always rejected.
    """
    ledger_path = Path(ledger_path)
    with _held_ledger_lock(ledger_path):
        entries = _read_ledger(ledger_path)
        match = next(
            (
                entry
                for entry in entries
                if isinstance(entry, dict) and entry.get("token") == token
            ),
            None,
        )
        if match is None:
            raise HarnessError(
                "AUTHORIZATION_TOKEN_MISSING: no human-issued authorization "
                "token matches the supplied --authorization value. A fresh, "
                "single-use token (see the 'issue-token' command) is "
                "required for every new run attempt, regardless of whether "
                "a previous attempt already spent quota, failed a preflight "
                "check, or errored."
            )
        if match.get("consumed"):
            raise HarnessError(
                "AUTHORIZATION_TOKEN_ALREADY_CONSUMED: this token was "
                "already consumed by a prior run attempt "
                f"(consumed_at={match.get('consumed_at')}). A fresh, "
                "single-use token is required for every new run attempt, "
                "regardless of whether a previous attempt already spent "
                "quota, failed a preflight check, or errored."
            )
        match["consumed"] = True
        match["consumed_at"] = _now_iso()
        _write_ledger(ledger_path, entries)


def require_authorization(
    token: str, ledger_path: Path = DEFAULT_TOKEN_LEDGER_PATH
) -> None:
    """CLI-facing authorization gate for the quota-consuming ``run`` command."""
    consume_authorization_token(token, ledger_path)


# -- scc-ux6: output-path locking --------------------------------------------
#
# No lock previously existed on --output, so two concurrent `run` invocations
# targeting the same path raced with no protection. This ties an exclusive,
# PID- and liveness-checked lock to the exact output path: a second launch
# against the same path while a live holder exists is rejected immediately,
# before any token check, preflight check, or subprocess spawn. The lock
# file itself is a plain, owner-readable JSON file -- read-only inspection
# (reading its contents, or the holder PID's argv via ps/lsof) is never
# gated by anything introduced here.


def _lock_path_for_output(output_path: Path) -> Path:
    output_path = Path(output_path)
    return output_path.with_name(output_path.name + ".lock")


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # The process exists but is owned by someone else: still a live holder.
        return True
    return True


def _read_lock_holder(lock_path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


@dataclass
class OutputLockHandle:
    lock_path: Path

    def release(self) -> None:
        try:
            os.unlink(self.lock_path)
        except FileNotFoundError:
            pass


def acquire_output_lock(output_path: Path) -> OutputLockHandle:
    """Atomically acquire an exclusive lock tied to ``output_path``.

    Fails closed with OUTPUT_PATH_LOCKED, naming the holder's PID and start
    time, when a live process already holds this exact output path's lock.
    A lock left behind by a dead or unreadable holder is reclaimed rather
    than treated as a permanent block.
    """
    output_path = Path(output_path)
    lock_path = _lock_path_for_output(output_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(
            {
                "pid": os.getpid(),
                "started_at": _now_iso(),
                "output_path": str(output_path),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            holder = _read_lock_holder(lock_path)
            holder_pid = holder.get("pid") if holder else None
            if isinstance(holder_pid, int) and _process_alive(holder_pid):
                started_at = (
                    holder.get("started_at", "unknown") if holder else "unknown"
                )
                raise HarnessError(
                    "OUTPUT_PATH_LOCKED: another run is already active for "
                    f"this --output path (holder PID {holder_pid}, started "
                    f"at {started_at}). Wait for it to finish, inspect "
                    f"{lock_path} directly, or choose a different --output "
                    "path."
                )
            # Stale lock: the recorded holder is gone, dead, or unreadable.
            try:
                os.unlink(lock_path)
            except FileNotFoundError:
                pass
            continue
        else:
            try:
                os.write(fd, payload)
                os.fsync(fd)
            finally:
                os.close(fd)
            return OutputLockHandle(lock_path=lock_path)


def _isolated_env(home: Path, hermes_source: Path) -> dict[str, str]:
    keep = ("PATH", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR", "TMPDIR")
    env = {key: os.environ[key] for key in keep if key in os.environ}
    fake_user_home = home.parent / "user-home"
    fake_user_home.mkdir(mode=0o700, exist_ok=True)
    env.update(
        {
            "HOME": str(fake_user_home),
            "HERMES_HOME": str(home),
            "PYTHONPATH": str(hermes_source),
            "NO_COLOR": "1",
            "HERMES_PLATFORM": "cli",
            "HERMES_SESSION_PLATFORM": "cli",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    return env


def _sandbox_literal(path: Path) -> str:
    return str(Path(path).resolve()).replace("\\", "\\\\").replace('"', '\\"')


def sandboxed_command(
    command: Sequence[str], *, runtime_root: Path, default_home: Path
) -> list[str]:
    """Wrap a command in a kernel-enforced denial for default-profile writes."""
    executable = shutil.which("sandbox-exec")
    if not executable:
        raise HarnessError("FILESYSTEM_SANDBOX_UNAVAILABLE")
    runtime_root = Path(runtime_root).resolve()
    default_home = Path(default_home).resolve()
    profile = runtime_root / "hermes-discovery.sb"
    profile.write_text(
        "(version 1)\n"
        "(allow default)\n"
        f'(allow file-write* (subpath "{_sandbox_literal(runtime_root)}"))\n'
        f'(deny file-write* (subpath "{_sandbox_literal(default_home)}"))\n',
        encoding="utf-8",
    )
    os.chmod(profile, 0o600)
    return [executable, "-f", str(profile), *command]


def verify_default_write_denial(runtime_root: Path, default_home: Path) -> None:
    """Prove the OS sandbox blocks a write beneath the real default profile."""
    canary = Path(default_home) / f".hermes-discovery-write-canary-{os.getpid()}"
    if canary.exists() or canary.is_symlink():
        raise HarnessError("DEFAULT_PROFILE_CANARY_COLLISION")
    command = sandboxed_command(
        ["/usr/bin/touch", str(canary)],
        runtime_root=runtime_root,
        default_home=default_home,
    )
    completed = subprocess.run(command, capture_output=True, check=False, timeout=15)
    if completed.returncode == 0 or canary.exists() or canary.is_symlink():
        raise HarnessError("DEFAULT_PROFILE_WRITE_SANDBOX_FAILED")


def _copy_credentials(template: Path | None, home: Path) -> None:
    if template is None:
        return
    template = Path(template)
    for name in _CREDENTIAL_FILES:
        source = template / name
        if not source.exists():
            continue
        if source.is_symlink() or not source.is_file():
            raise HarnessError("CREDENTIAL_FILE_INVALID")
        target = home / name
        shutil.copyfile(source, target, follow_symlinks=False)
        os.chmod(target, 0o600)


def _hermes_executable(hermes_source: Path) -> Path:
    executable = Path(hermes_source) / "venv" / "bin" / "hermes"
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise HarnessError("HERMES_EXECUTABLE_UNAVAILABLE")
    return executable


def _parse_tool_events(state_db: Path) -> int:
    if not state_db.is_file():
        return 0
    count = 0
    try:
        with sqlite3.connect(f"file:{state_db}?mode=ro", uri=True) as connection:
            rows = connection.execute(
                "SELECT tool_calls FROM messages WHERE role = 'assistant' AND tool_calls IS NOT NULL"
            ).fetchall()
    except (sqlite3.Error, OSError) as error:
        raise HarnessError("SESSION_EVENT_READ_FAILED") from error
    for (raw_calls,) in rows:
        try:
            calls = json.loads(raw_calls) if isinstance(raw_calls, str) else raw_calls
        except json.JSONDecodeError:
            continue
        if not isinstance(calls, list):
            continue
        for call in calls:
            function = call.get("function", {}) if isinstance(call, dict) else {}
            if function.get("name") != "skill_view":
                continue
            arguments = function.get("arguments", {})
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    arguments = {}
            if isinstance(arguments, dict) and arguments.get("name") == "beads":
                count += 1
    return count


def _load_event_count(home: Path) -> int:
    usage_path = home / "skills" / ".usage.json"
    if not usage_path.is_file():
        return 0
    usage = _load_json(usage_path, "SKILL_USAGE_INVALID")
    record = usage.get("beads", {}) if isinstance(usage, dict) else {}
    value = record.get("use_count", 0) if isinstance(record, dict) else 0
    return value if isinstance(value, int) and value >= 0 else 0


def _retain_stream(
    retention_root: Path, scenario_id: str, stream: str, text: str
) -> str:
    """Persist a truncated copy of one errored lane's stream at mode 0600.

    The text can carry provider messages and session identifiers, so it is
    written to the 0700 runtime root and never echoed to stdout. Only the
    path is reported. The retained file never exceeds
    ``RAW_RETENTION_MAX_BYTES``, and the marker that records the clip is
    counted inside that budget, so a reader can never mistake a truncated
    stream for a complete one nor a capped file for an oversized one.
    """
    retention_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    raw = text.encode("utf-8", errors="replace")
    if len(raw) > RAW_RETENTION_MAX_BYTES:
        marker = f"\n[truncated: {len(raw)} bytes of raw output clipped]\n".encode()
        keep = max(0, RAW_RETENTION_MAX_BYTES - len(marker))
        raw = raw[:keep] + marker
    path = retention_root / f"{scenario_id}.{stream}.txt"
    _write_secure_bytes(path, raw)
    return str(path)


def _run_scenario(
    scenario: Scenario,
    *,
    lane: Path,
    candidate: Path,
    candidate_sha256: str,
    hermes_source: Path,
    provider: str,
    model: str,
    credentials: Path | None,
    default_home: Path,
    retention_root: Path | None = None,
) -> ScenarioResult:
    home = lane / "hermes-home"
    installed = install_candidate(candidate, home, candidate_sha256)
    _copy_credentials(credentials, home)
    env = _isolated_env(home, hermes_source)
    command = [
        str(_hermes_executable(hermes_source)),
        "chat",
        "--query-file",
        "-",
        "--oneshot",
        "--quiet",
        "--toolsets",
        "skills",
        "--provider",
        provider,
        "--model",
        model,
        "--max-turns",
        str(MAX_TURNS),
        "--run-budget",
        str(RUN_BUDGET_SECONDS),
        "--ignore-user-config",
        "--ignore-rules",
        "--source",
        "tool",
    ]
    completed = subprocess.run(
        sandboxed_command(command, runtime_root=lane, default_home=default_home),
        input=scenario.prompt,
        text=True,
        capture_output=True,
        check=False,
        cwd=lane,
        env=env,
        timeout=RUN_BUDGET_SECONDS + 30,
    )
    state_db = home / "state.db"
    # Retain only for errored lanes. A clean lane is already fully described by
    # its hashes and event counts; an errored lane is the one case where the
    # text itself is the evidence, and discarding it forces a fresh (and
    # possibly billed) reproduction to diagnose. The paths are resolved before
    # the result is built because ScenarioResult is frozen.
    retain = retention_root is not None and completed.returncode != 0
    result = ScenarioResult(
        scenario_id=scenario.scenario_id,
        split=scenario.split,
        expected_load=scenario.expected_load,
        discovery_events=_parse_tool_events(state_db),
        load_events=_load_event_count(home),
        exit_code=completed.returncode,
        stdout_sha256=sha256_bytes(completed.stdout.encode()),
        stderr_sha256=sha256_bytes(completed.stderr.encode()),
        state_sha256=sha256_file(state_db) if state_db.is_file() else sha256_bytes(b""),
        raw_stdout_path=(
            _retain_stream(
                retention_root, scenario.scenario_id, "stdout", completed.stdout
            )
            if retain
            else None
        ),
        raw_stderr_path=(
            _retain_stream(
                retention_root, scenario.scenario_id, "stderr", completed.stderr
            )
            if retain
            else None
        ),
    )
    if hash_tree(installed) != candidate_sha256:
        raise HarnessError("INSTALLED_CANDIDATE_MUTATED")
    return result


# The 16 tracked roots of a Hermes profile home. This is the canonical set
# referenced throughout scc-l41's hardening: any fingerprint mutation must be
# attributable to exactly one of these names, never just a run-level bool.
_PROFILE_ROOTS: tuple[str, ...] = (
    "config.yaml",
    ".env",
    "auth.json",
    "active_profile",
    "SOUL.md",
    "state.db",
    "state.db-wal",
    "state.db-shm",
    "skills",
    "memories",
    "sessions",
    "plugins",
    "hooks",
    "cron",
    "checkpoints",
    "backups",
)
# state.db-shm is SQLite's memory-mapped WAL-index file: shared-memory
# bookkeeping (including a connection-local salt used to detect stale
# readers) that can differ, byte for byte, between two snapshots even when
# a checkpoint succeeded both times and nothing in the database actually
# changed. It is never meaningful to compare and is always excluded.
_SHM_ROOT_NAME = "state.db-shm"
# state.db-wal, by contrast, becomes a deterministic zero-length file after
# a successful TRUNCATE checkpoint (see _checkpoint_wal), so on the happy
# path it is safe -- and useful -- to compare directly. It is only excluded
# -- the documented fallback -- when a checkpoint could not be attempted
# for either side of a pair, since an un-checkpointed WAL's content is
# inherently racy against ongoing writes.
_WAL_ROOT_NAME = "state.db-wal"
_PROFILE_HOME_MISSING = "__profile_home_missing__"


@dataclass(frozen=True)
class ProfileFingerprint:
    """A per-root structural snapshot of one Hermes profile home.

    ``roots`` maps each of the ``_PROFILE_ROOTS`` names that was present (or
    a symlink) at snapshot time to a SHA-256 structural hash over its
    relative path, symlink target, and file bytes -- never the raw bytes or
    path contents themselves, so nothing built from this can leak
    config.yaml/.env/auth.json secrets (only root names and hashes ever
    reach a report).

    ``wal_checkpointed`` records whether ``_checkpoint_wal`` was able to
    normalize state.db-wal/state.db-shm immediately before this snapshot was
    taken. ``_profile_diff`` uses it to decide whether those two roots are
    safe to compare (see its docstring for the fallback).
    """

    roots: Mapping[str, str]
    wal_checkpointed: bool


def _checkpoint_wal(home: Path) -> bool:
    """Best-effort SQLite WAL checkpoint immediately before a snapshot.

    A successful ``TRUNCATE`` checkpoint folds any pending state.db-wal
    frames into state.db and truncates the WAL file, so an ordinary
    write-then-checkpoint cycle does not read as a content change between
    two ``_profile_fingerprint`` snapshots. This is the happy-path WAL/SHM
    false-positive suppression required by scc-l41.

    Returns True when the checkpoint ran -- including the trivial case
    where state.db does not exist at all, so there is nothing to
    checkpoint and no possible WAL churn -- and False when a checkpoint
    could not be attempted, for example because the database is locked by
    another connection or the file is not a valid SQLite database.

    Documented fallback: when this returns False for either side of a
    before/after pair, ``_profile_diff`` excludes state.db-wal/state.db-shm
    from the equality check entirely for that pair (an un-checkpointed
    WAL/SHM pair is inherently racy) and compares only state.db's own,
    possibly un-checkpointed, content instead.
    """
    state_db = Path(home) / "state.db"
    if not state_db.is_file():
        return True
    try:
        connection = sqlite3.connect(str(state_db), timeout=5)
        try:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            connection.commit()
        finally:
            connection.close()
    except sqlite3.Error:
        return False
    return True


def _profile_fingerprint(home: Path) -> ProfileFingerprint:
    """Snapshot a Hermes profile home across the 16 tracked roots.

    Before hashing, this checkpoints the SQLite WAL/SHM pair (see
    ``_checkpoint_wal``) so ordinary WAL churn on state.db does not read as
    a mutation when compared against another snapshot via ``_profile_diff``.
    Hashing itself never follows symlinks -- a symlink's target path is
    hashed in place of its content -- and never returns raw file bytes.
    """
    home = Path(home)
    wal_checkpointed = _checkpoint_wal(home)
    if not home.exists():
        return ProfileFingerprint(
            roots={_PROFILE_HOME_MISSING: sha256_bytes(b"missing")},
            wal_checkpointed=wal_checkpointed,
        )

    def fingerprint_path(path: Path) -> str:
        digest = hashlib.sha256()
        pending = [path]
        while pending:
            current = pending.pop()
            relative = current.relative_to(home).as_posix().encode()
            mode = current.lstat().st_mode
            digest.update(len(relative).to_bytes(8, "big"))
            digest.update(relative)
            if stat.S_ISLNK(mode):
                digest.update(b"L")
                digest.update(os.readlink(current).encode())
            elif stat.S_ISREG(mode):
                digest.update(b"F")
                digest.update(sha256_file(current).encode())
            elif stat.S_ISDIR(mode):
                digest.update(b"D")
                pending.extend(
                    sorted(current.iterdir(), key=lambda item: item.name, reverse=True)
                )
            else:
                digest.update(b"O")
                digest.update(str(stat.S_IFMT(mode)).encode())
        return digest.hexdigest()

    records: dict[str, str] = {}
    for name in _PROFILE_ROOTS:
        path = home / name
        if path.exists() or path.is_symlink():
            records[name] = fingerprint_path(path)
    return ProfileFingerprint(roots=records, wal_checkpointed=wal_checkpointed)


def _profile_diff(before: ProfileFingerprint, after: ProfileFingerprint) -> list[str]:
    """Sorted tracked-root names whose content differs between two snapshots.

    Root-level attribution: this is what lets a caller name which of the 16
    tracked roots changed instead of exposing only a boolean.

    WAL/SHM false-positive suppression: state.db-shm is always excluded --
    its raw bytes are volatile shared-memory bookkeeping that is never
    meaningfully comparable, checkpointed or not (see _SHM_ROOT_NAME).
    state.db-wal is compared on the happy path, where a successful
    checkpoint makes it a deterministic zero-length file. Documented
    fallback: if either snapshot's ``wal_checkpointed`` is False,
    state.db-wal is excluded too for this pair -- an un-checkpointed WAL's
    content is inherently racy, so only state.db's own (checkpointed, on
    the happy path) content is compared for that part of profile state.
    """
    exclude = {_SHM_ROOT_NAME}
    if not (before.wal_checkpointed and after.wal_checkpointed):
        exclude.add(_WAL_ROOT_NAME)
    names = (set(before.roots) | set(after.roots)) - exclude
    return sorted(
        name for name in names if before.roots.get(name) != after.roots.get(name)
    )


def build_report(
    results: Sequence[ScenarioResult],
    *,
    candidate_sha256: str,
    corpus_sha256: str,
    hermes_commit: str,
    provider: str,
    model: str,
    default_profile_unchanged: bool,
    default_profile_write_denied: bool = True,
    aborted_reason: str | None = None,
) -> dict[str, Any]:
    rows = []
    for result in results:
        row = asdict(result)
        row["passed"] = result.passed
        row["errored"] = result.errored
        row["outcome"] = result.outcome
        rows.append(row)
    # Strict three-way partition. "failed" counts routing failures only:
    # an errored lane never reached routing, so folding it into "failed"
    # would report a provider outage as proof the skill does not route.
    passed = sum(1 for row in rows if row["outcome"] == "passed")
    failed = sum(1 for row in rows if row["outcome"] == "failed")
    errored = sum(1 for row in rows if row["outcome"] == "errored")
    retained = any(
        row.get("raw_stdout_path") or row.get("raw_stderr_path") for row in rows
    )
    report = {
        "schema_version": REPORT_SCHEMA,
        "candidate_sha256": candidate_sha256,
        "corpus_sha256": corpus_sha256,
        "hermes_commit": hermes_commit,
        "provider": provider,
        "model": model,
        "scenario_count": len(rows),
        "passed": passed,
        "failed": failed,
        "errored": errored,
        "scored_count": passed + failed,
        "default_profile_unchanged": default_profile_unchanged,
        "default_profile_write_denied": default_profile_write_denied,
        "raw_content_retained": retained,
        "results": rows,
    }
    if aborted_reason is not None:
        report["aborted"] = True
        report["aborted_reason"] = aborted_reason
    return report


def run_corpus(
    *,
    scenarios: Sequence[Scenario],
    candidate: Path,
    candidate_sha256: str,
    corpus_sha256: str,
    hermes_source: Path,
    runtime_root: Path,
    default_home: Path,
    credentials: Path | None,
    provider: str,
    model: str,
    force_full_sweep: bool = False,
) -> dict[str, Any]:
    if len(scenarios) != SCENARIO_COUNT:
        raise HarnessError("CORPUS_INDEX_MISMATCH")
    verify_frozen_hermes(hermes_source)
    runtime_root = Path(runtime_root).resolve()
    default_home = Path(default_home).resolve()
    if (
        runtime_root == default_home
        or runtime_root in default_home.parents
        or default_home in runtime_root.parents
    ):
        raise HarnessError("RUNTIME_DEFAULT_PROFILE_ALIAS")
    runtime_root.mkdir(parents=True, exist_ok=False)
    os.chmod(runtime_root, 0o700)
    verify_default_write_denial(runtime_root, default_home)
    run_before_fingerprint = _profile_fingerprint(default_home)
    # Retention lives under the 0700 runtime root, not the lane: each lane is
    # torn down immediately after its run, so anything written inside it is
    # gone before the report is built.
    retention_root = runtime_root / "retained"
    results: list[ScenarioResult] = []
    retained_lanes = 0
    aborted_reason: str | None = None
    try:
        for index, scenario in enumerate(scenarios):
            lane = runtime_root / scenario.scenario_id
            lane.mkdir(mode=0o700)
            # Per-lane fingerprinting (scc-l41): a snapshot immediately before
            # and after this exact lane, so a mutation is attributable to the
            # one scenario that caused it instead of only "somewhere in the
            # 72-lane run".
            lane_before_fingerprint = _profile_fingerprint(default_home)
            try:
                result = _run_scenario(
                    scenario,
                    lane=lane,
                    candidate=candidate,
                    candidate_sha256=candidate_sha256,
                    hermes_source=hermes_source,
                    provider=provider,
                    model=model,
                    credentials=credentials,
                    default_home=default_home,
                    retention_root=(
                        retention_root
                        if retained_lanes < RAW_RETENTION_LANE_LIMIT
                        else None
                    ),
                )
            finally:
                shutil.rmtree(lane, ignore_errors=False)
            lane_after_fingerprint = _profile_fingerprint(default_home)
            result = replace(
                result,
                profile_delta=_profile_diff(
                    lane_before_fingerprint, lane_after_fingerprint
                ),
            )
            results.append(result)
            if result.raw_stdout_path or result.raw_stderr_path:
                retained_lanes += 1
            # Pre-flight: the first lane is a canary. If it errors, the cause
            # is almost always systemic (an exhausted provider quota, a broken
            # install), and running the remaining lanes only repeats the same
            # failure at full cost. Abort fail-closed and say why. Routing
            # failures never trigger this -- only errors, which mean the
            # session never reached routing at all.
            if index == 0 and result.errored and not force_full_sweep:
                aborted_reason = (
                    "PREFLIGHT_LANE_ERRORED: the first lane exited "
                    f"{result.exit_code} without completing. This is treated as "
                    "a systemic failure, so the remaining lanes were skipped. "
                    "Inspect the retained raw output, then re-run with "
                    "--force-full-sweep to override."
                )
                break
    finally:
        run_after_fingerprint = _profile_fingerprint(default_home)
    # default_profile_unchanged stays a single run-level boolean for backward
    # compatibility (existing consumers key off it directly), but its meaning
    # is unchanged: True only if no tracked root differed anywhere across the
    # whole run. It is now derived from the conjunction of every lane's own
    # before/after delta plus the whole-run bookend snapshots, rather than
    # only the bookend snapshots, so a mutate-then-revert within a single
    # lane can no longer cancel out and hide behind a run-level match.
    run_level_changed_roots = _profile_diff(
        run_before_fingerprint, run_after_fingerprint
    )
    unchanged = not run_level_changed_roots and all(
        not result.profile_delta for result in results
    )
    report = build_report(
        results,
        candidate_sha256=candidate_sha256,
        corpus_sha256=corpus_sha256,
        hermes_commit=FROZEN_HERMES_COMMIT,
        provider=provider,
        model=model,
        default_profile_unchanged=unchanged,
        aborted_reason=aborted_reason,
    )
    return report


def probe_actual_hermes_install(
    *,
    candidate: Path,
    candidate_sha256: str,
    hermes_source: Path,
    runtime_root: Path,
    default_home: Path,
) -> dict[str, Any]:
    """Exercise actual frozen Hermes scan/load code without any model call."""
    verify_frozen_hermes(hermes_source)
    runtime_root = Path(runtime_root).resolve()
    default_home = Path(default_home).resolve()
    if (
        runtime_root == default_home
        or runtime_root in default_home.parents
        or default_home in runtime_root.parents
    ):
        raise HarnessError("RUNTIME_DEFAULT_PROFILE_ALIAS")
    runtime_root.mkdir(parents=True, exist_ok=False)
    os.chmod(runtime_root, 0o700)
    verify_default_write_denial(runtime_root, default_home)
    home = runtime_root / "hermes-home"
    installed = install_candidate(candidate, home, candidate_sha256)
    before = _profile_fingerprint(default_home)
    probe_code = r"""
import json
from tools.skills_tool import skills_list
from model_tools import handle_function_call
listed = json.loads(skills_list())
names = [row.get("name") for row in listed.get("skills", [])]
loaded = handle_function_call(
    "skill_view", {"name": "beads"}, task_id="routing-probe", session_id="routing-probe"
)
try:
    payload = json.loads(loaded)
except Exception:
    payload = {}
print(json.dumps({"candidate_discovered": "beads" in names, "candidate_loaded": payload.get("success") is True}))
"""
    python = Path(hermes_source) / "venv" / "bin" / "python"
    try:
        completed = subprocess.run(
            sandboxed_command(
                [str(python), "-c", probe_code],
                runtime_root=runtime_root,
                default_home=default_home,
            ),
            capture_output=True,
            check=False,
            text=True,
            cwd=runtime_root,
            env=_isolated_env(home, hermes_source),
            timeout=60,
        )
        if completed.returncode:
            raise HarnessError("HERMES_PROBE_FAILED")
        try:
            payload = json.loads(completed.stdout.strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError) as error:
            raise HarnessError("HERMES_PROBE_OUTPUT_INVALID") from error
        after = _profile_fingerprint(default_home)
        evidence = {
            "schema_version": "hermes-beads-install-probe.v1",
            "hermes_commit": FROZEN_HERMES_COMMIT,
            "candidate_sha256": candidate_sha256,
            "installed_candidate_sha256": hash_tree(installed),
            "candidate_discovered": payload.get("candidate_discovered") is True,
            "candidate_loaded": payload.get("candidate_loaded") is True,
            "load_events": _load_event_count(home),
            "default_profile_unchanged": not _profile_diff(before, after),
            "default_profile_write_denied": True,
            "default_profile_mutation_by_harness": False,
            "automatic_routing_exercised": False,
            "blocker": "MODEL_EXECUTION_NOT_AUTHORIZED",
            "raw_content_retained": False,
        }
        return evidence
    finally:
        shutil.rmtree(runtime_root, ignore_errors=False)


def _write_secure_bytes(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` atomically with mode exactly 0o600.

    ``os.open``'s mode argument is masked by the process umask, so a hostile
    or merely permissive umask (e.g. 0o000) can leave the file group/world
    readable even when 0o600 is requested at open time. The explicit
    ``os.chmod`` after the atomic rename is what actually pins the mode,
    regardless of umask (Defect 4).
    """
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def _write_json(path: Path | None, payload: Mapping[str, Any]) -> None:
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if path is None:
        sys.stdout.write(rendered)
    else:
        _write_secure_bytes(Path(path), rendered.encode("utf-8"))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    digest = sub.add_parser("candidate-hash")
    digest.add_argument("candidate", type=Path)

    probe = sub.add_parser("probe")
    probe.add_argument("--candidate", type=Path, required=True)
    probe.add_argument("--candidate-sha256", required=True)
    probe.add_argument("--hermes-source", type=Path, required=True)
    probe.add_argument("--runtime-root", type=Path, required=True)
    probe.add_argument("--default-home", type=Path, required=True)
    probe.add_argument("--output", type=Path)

    plan = sub.add_parser("plan")
    for command in (plan,):
        command.add_argument("--provider", default="openai-codex")
        command.add_argument("--model", default="gpt-5.6-sol")

    issue_token = sub.add_parser(
        "issue-token",
        help=(
            "Mint one fresh, single-use authorization token for a single "
            "future 'run' invocation. Run this yourself, as a human -- an "
            "agent must never call it to self-authorize."
        ),
    )
    issue_token.add_argument(
        "--token-ledger", type=Path, default=DEFAULT_TOKEN_LEDGER_PATH
    )
    issue_token.add_argument("--output", type=Path)

    run = sub.add_parser("run")
    run.add_argument("--candidate", type=Path, required=True)
    run.add_argument("--candidate-sha256", required=True)
    run.add_argument("--corpus", type=Path, required=True)
    run.add_argument("--corpus-sha256", required=True)
    run.add_argument("--design", type=Path, required=True)
    run.add_argument("--hermes-source", type=Path, required=True)
    run.add_argument("--runtime-root", type=Path, required=True)
    run.add_argument("--default-home", type=Path, required=True)
    run.add_argument("--credentials", type=Path)
    run.add_argument("--provider", default="openai-codex")
    run.add_argument("--model", default="gpt-5.6-sol")
    run.add_argument(
        "--authorization",
        required=True,
        help=(
            "A single-use token minted by 'issue-token', not free-text approval prose."
        ),
    )
    run.add_argument("--token-ledger", type=Path, default=DEFAULT_TOKEN_LEDGER_PATH)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument(
        "--force-full-sweep",
        action="store_true",
        help=(
            "Run every lane even when the first lane errors. Off by default: "
            "a systemic failure would otherwise repeat across all "
            f"{SCENARIO_COUNT} lanes at full provider cost."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "candidate-hash":
            print(hash_tree(args.candidate))
            return 0
        if args.command == "plan":
            _write_json(None, approval_request(args.provider, args.model))
            return 0
        if args.command == "issue-token":
            token = issue_authorization_token(args.token_ledger)
            _write_json(
                args.output,
                {"token": token, "token_ledger": str(Path(args.token_ledger))},
            )
            return 0
        if args.command == "probe":
            payload = probe_actual_hermes_install(
                candidate=args.candidate,
                candidate_sha256=args.candidate_sha256,
                hermes_source=args.hermes_source,
                runtime_root=args.runtime_root,
                default_home=args.default_home,
            )
            _write_json(args.output, payload)
            return 0
        # command == "run": lock the exact --output path first -- a second
        # invocation racing for the same path is rejected before it ever
        # touches the token ledger, a preflight check, or a subprocess. Only
        # once the lock is held do we require and consume a real, single-use
        # authorization token, before any preflight check or subprocess spawn.
        lock = acquire_output_lock(args.output)
        try:
            require_authorization(args.authorization, args.token_ledger)
            scenarios = load_corpus(args.corpus, args.design, args.corpus_sha256)
            payload = run_corpus(
                scenarios=scenarios,
                candidate=args.candidate,
                candidate_sha256=args.candidate_sha256,
                corpus_sha256=args.corpus_sha256,
                hermes_source=args.hermes_source,
                runtime_root=args.runtime_root,
                default_home=args.default_home,
                credentials=args.credentials,
                provider=args.provider,
                model=args.model,
                force_full_sweep=args.force_full_sweep,
            )
            _write_json(args.output, payload)
            # Exit codes are a three-way partition, matching the report. An
            # errored or aborted sweep must never exit 0: with the errored
            # count split out of "failed", a total provider outage leaves
            # failed == 0, and reporting that as success is exactly the
            # confusion this change exists to remove. 4 means "no verdict";
            # 3 means "a real routing failure was measured".
            if payload["errored"] or payload.get("aborted"):
                return 4
            return 0 if payload["failed"] == 0 else 3
        finally:
            lock.release()
    except HarnessError as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
