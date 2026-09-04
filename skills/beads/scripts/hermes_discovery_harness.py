#!/usr/bin/env python3
"""Run hash-bound Hermes skill routing checks in throwaway profile homes.

The no-model ``probe`` exercises the frozen Hermes scanner and ``skill_view``
handler. The ``run`` command is intentionally authorization-gated because real
automatic routing is an LLM decision. It accepts private prompts over stdin,
retains only hashes and event counts, and removes every per-scenario home.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
from dataclasses import asdict, dataclass
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


def _canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


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
    text = f"authorize 72 candidate discovery runs via {provider}/{model}"
    return {
        "required_authorization_text": text,
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


def require_authorization(value: str, provider: str, model: str) -> None:
    if value != approval_request(provider, model)["required_authorization_text"]:
        raise HarnessError("MODEL_RUNS_NOT_AUTHORIZED")


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


def _profile_fingerprint(home: Path) -> str:
    """Hash profile state, including symlink identity, without following links."""
    home = Path(home)
    if not home.exists():
        return sha256_bytes(b"missing")

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

    roots = [
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
    ]
    records: list[tuple[str, str]] = []
    for name in roots:
        path = home / name
        if path.exists() or path.is_symlink():
            records.append((name, fingerprint_path(path)))
    return sha256_bytes(_canonical_bytes(records))


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
    before = _profile_fingerprint(default_home)
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
        after = _profile_fingerprint(default_home)
    unchanged = before == after
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
            "default_profile_unchanged": before == after,
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
    run.add_argument("--authorization", required=True)
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
        require_authorization(args.authorization, args.provider, args.model)
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
        # errored or aborted sweep must never exit 0: with the errored count
        # split out of "failed", a total provider outage leaves failed == 0,
        # and reporting that as success is exactly the confusion this change
        # exists to remove. 4 means "no verdict"; 3 means "a real routing
        # failure was measured".
        if payload["errored"] or payload.get("aborted"):
            return 4
        return 0 if payload["failed"] == 0 else 3
    except HarnessError as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
