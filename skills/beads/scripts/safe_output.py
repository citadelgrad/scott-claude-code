#!/usr/bin/env python3
"""Bounded, no-shell subprocess capture with sanitize-before-persist.

Python recommends argument sequences and full executable paths; unbounded
``communicate`` buffers in memory. See https://docs.python.org/3/library/subprocess.html
OWASP Logging: https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
import threading
import time
import unicodedata
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Literal, TypeVar

sys.path.insert(0, str(Path(__file__).parent))
import schema_runtime

MAX_ARGC = 64
MAX_ARG_BYTES = 4096
MAX_ARGV_BYTES = 32768
MAX_STDIN_BYTES = 65536
MAX_STREAM_BYTES = 4 * 1024 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024
MAX_TIMEOUT_SECONDS = 3600
READ_CHUNK_BYTES = 65536
T = TypeVar("T")


class SafeOutputError(ValueError):
    pass


@dataclass(frozen=True)
class CommandSpec:
    profile: Literal[
        "hermetic", "beads", "verification", "git_local", "dolt_local", "recovery_probe"
    ]
    argv: tuple[str, ...]
    cwd: Path
    stdin_bytes: bytes | None
    timeout_seconds: int
    stdout_limit_bytes: int
    stderr_limit_bytes: int
    total_limit_bytes: int
    output_codec: Literal["utf8_text", "utf8_json", "discard"]
    stdout_log: Path | None
    stderr_log: Path | None


@dataclass(frozen=True, repr=False)
class SensitiveSet:
    values: tuple[str, ...]


@dataclass(frozen=True)
class SafeArtifact:
    path: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class SanitizedValue:
    status: Literal["OK", "REDACTED", "REDACTION_FAILED"]
    value: str | None
    redaction_count: int
    error_code: str | None


@dataclass(frozen=True)
class SafeCommandResult:
    schema_version: Literal["beads.safe-command-result.v1"]
    profile: str
    status: str
    exit_code: int | None
    signal: int | None
    redaction_count: int
    stdout_log: SafeArtifact | None
    stderr_log: SafeArtifact | None
    started_at: str
    finished_at: str
    error_code: str | None
    argv_sha256: str
    stdout_bytes: int | None
    stderr_bytes: int | None


_SAFE_ENV = {
    "LC_ALL": "C.UTF-8",
    "LANG": "C.UTF-8",
    "TZ": "UTC",
    "PYTHONIOENCODING": "utf-8:strict",
    "NO_COLOR": "1",
    "TERM": "dumb",
    "PAGER": "cat",
    "GIT_TERMINAL_PROMPT": "0",
    "GCM_INTERACTIVE": "never",
    "SSH_ASKPASS_REQUIRE": "never",
}
_INHERITED = {
    "hermetic": (),
    "beads": ("HOME", "USER", "LOGNAME", "TMPDIR"),
    "verification": (
        "HOME",
        "USER",
        "LOGNAME",
        "TMPDIR",
        "PATH",
        "VIRTUAL_ENV",
        "UV_CACHE_DIR",
        "CARGO_HOME",
        "RUSTUP_HOME",
        "SDKROOT",
        "DEVELOPER_DIR",
    ),
    "git_local": ("HOME", "USER", "LOGNAME", "TMPDIR"),
    "dolt_local": ("HOME", "USER", "LOGNAME", "TMPDIR"),
    "recovery_probe": ("HOME", "USER", "LOGNAME", "TMPDIR"),
}
_BIDI = {
    chr(code)
    for code in (
        *range(0x202A, 0x202F),
        *range(0x2066, 0x206A),
        0x200B,
        0x200C,
        0x200D,
        0xFEFF,
    )
}
_LABEL = re.compile(
    r"(?i)(authorization|proxy-authorization|cookie|set-cookie|password|passwd|secret|token|api[-_ ]?key|access[-_ ]?key|private[-_ ]?key|client[-_ ]?secret)\s*[:=]\s*(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;]+)"
)
_TOKEN = re.compile(
    r"(?i)(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}|(?:gh[opsu]_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})"
)
_URL_AUTH = re.compile(r"(?i)https?://[^\s/@:]+:[^\s/@]+@")
_PEM = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL
)
_ASCII_SENSITIVE_LABEL = re.compile(
    r"(?i)\b(?:password|passwd|secret|token|api[-_ ]?key|access[-_ ]?key|private[-_ ]?key|client[-_ ]?secret)\b"
)


def _timestamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _contains_bad_unicode(value: str) -> bool:
    return "\x00" in value or any(
        0xD800 <= ord(ch) <= 0xDFFF or ch in _BIDI for ch in value
    )


def is_sensitive_label(value: str) -> bool:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return bool(_ASCII_SENSITIVE_LABEL.search(normalized))


# Unicode classification follows the security mechanisms in UTS #39; normalization
# is classification-only and never changes persisted ordinary text or identities.
# https://www.unicode.org/reports/tr39/
def sanitize_value(
    value: str, *, sensitive: SensitiveSet, max_utf8_bytes: int
) -> SanitizedValue:
    try:
        if len(value.encode("utf-8", "strict")) > max_utf8_bytes:
            return SanitizedValue("REDACTION_FAILED", None, 0, "OUTPUT_TOO_LARGE")
        if _contains_bad_unicode(value):
            return SanitizedValue("REDACTION_FAILED", None, 0, "AMBIGUOUS_UNICODE")
        normalized = unicodedata.normalize("NFKC", value).casefold()
        if is_sensitive_label(normalized) and not _ASCII_SENSITIVE_LABEL.search(value):
            return SanitizedValue(
                "REDACTION_FAILED", None, 0, "AMBIGUOUS_CREDENTIAL_LABEL"
            )
        secrets = sorted(set(sensitive.values), key=lambda item: (-len(item), item))
        if any(not item or len(item.encode("utf-8")) < 8 for item in secrets):
            return SanitizedValue(
                "REDACTION_FAILED", None, 0, "SENSITIVE_VALUE_AMBIGUOUS"
            )
        clean = value
        count = 0
        for secret in secrets:
            occurrences = clean.count(secret)
            if occurrences:
                clean = clean.replace(secret, "[REDACTED]")
                count += occurrences
        for pattern in (_PEM, _LABEL, _TOKEN, _URL_AUTH):
            clean, found = pattern.subn("[REDACTED]", clean)
            count += found
        if any(secret in clean for secret in secrets) or _contains_bad_unicode(clean):
            return SanitizedValue(
                "REDACTION_FAILED", None, 0, "REDACTION_RESCAN_FAILED"
            )
        return SanitizedValue("REDACTED" if count else "OK", clean, count, None)
    except (UnicodeError, re.error):
        return SanitizedValue("REDACTION_FAILED", None, 0, "REDACTION_FAILED")


def _validate_spec(spec: CommandSpec, sensitive: SensitiveSet) -> None:
    if spec.profile not in _INHERITED or not spec.argv or len(spec.argv) > MAX_ARGC:
        raise SafeOutputError("INVALID_COMMAND_SPEC")
    if not Path(spec.argv[0]).is_absolute() or not Path(spec.argv[0]).is_file():
        raise SafeOutputError("EXECUTABLE_NOT_ABSOLUTE_FILE")
    encoded = []
    for arg in spec.argv:
        if _contains_bad_unicode(arg):
            raise SafeOutputError("INVALID_ARGV")
        raw = arg.encode("utf-8")
        if not raw or len(raw) > MAX_ARG_BYTES:
            raise SafeOutputError("INVALID_ARGV")
        encoded.append(raw)
    if sum(map(len, encoded)) > MAX_ARGV_BYTES:
        raise SafeOutputError("INVALID_ARGV")
    if spec.stdin_bytes is not None and len(spec.stdin_bytes) > MAX_STDIN_BYTES:
        raise SafeOutputError("STDIN_TOO_LARGE")
    if not spec.cwd.is_absolute() or not spec.cwd.is_dir() or spec.cwd.is_symlink():
        raise SafeOutputError("INVALID_CWD")
    try:
        if spec.cwd.resolve(strict=True) != spec.cwd:
            raise SafeOutputError("INVALID_CWD")
    except OSError as exc:
        raise SafeOutputError("INVALID_CWD") from exc
    if not 1 <= spec.timeout_seconds <= MAX_TIMEOUT_SECONDS:
        raise SafeOutputError("INVALID_TIMEOUT")
    if (
        not 1 <= spec.stdout_limit_bytes <= MAX_STREAM_BYTES
        or not 1 <= spec.stderr_limit_bytes <= MAX_STREAM_BYTES
        or not 1 <= spec.total_limit_bytes <= MAX_TOTAL_BYTES
    ):
        raise SafeOutputError("INVALID_OUTPUT_LIMIT")
    if spec.total_limit_bytes < max(spec.stdout_limit_bytes, spec.stderr_limit_bytes):
        raise SafeOutputError("INVALID_OUTPUT_LIMIT")
    if any(not value or len(value.encode("utf-8")) < 8 for value in sensitive.values):
        raise SafeOutputError("SENSITIVE_VALUE_AMBIGUOUS")


def _environment(profile: str) -> dict[str, str]:
    env = dict(_SAFE_ENV)
    for name in _INHERITED[profile]:
        value = os.environ.get(name)
        if value is not None:
            if _contains_bad_unicode(value) or "\n" in value or "\r" in value:
                raise SafeOutputError("UNSAFE_ENVIRONMENT")
            env[name] = value
    return env


# Same-directory exclusive creation, fsync, and atomic publication use the
# descriptor-level APIs documented at https://docs.python.org/3/library/os.html#os.open
def _write_all(fd: int, data: bytes) -> None:
    remaining = memoryview(data)
    while remaining:
        written = os.write(fd, remaining)
        if written <= 0:
            raise OSError("short artifact write")
        remaining = remaining[written:]


def _atomic_write(path: Path, data: bytes) -> SafeArtifact:
    parent = path.parent
    if not path.is_absolute() or path.exists() or path.is_symlink():
        raise SafeOutputError("UNSAFE_LOG_PATH")
    try:
        canonical_parent = parent.resolve(strict=True)
    except OSError as exc:
        raise SafeOutputError("UNSAFE_LOG_PATH") from exc
    if canonical_parent != parent:
        raise SafeOutputError("UNSAFE_LOG_PATH")
    st = parent.stat(follow_symlinks=False)
    if (
        not stat.S_ISDIR(st.st_mode)
        or st.st_uid != os.getuid()
        or stat.S_IMODE(st.st_mode) & 0o022
    ):
        raise SafeOutputError("UNSAFE_LOG_PARENT")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    temporary = parent / f".{path.name}.tmp-{os.getpid()}-{time.monotonic_ns()}"
    fd: int | None = None
    try:
        fd = os.open(temporary, flags, 0o600)
        _write_all(fd, data)
        os.fsync(fd)
        os.fchmod(fd, 0o600)
        os.close(fd)
        fd = None
        os.link(temporary, path, follow_symlinks=False)
        directory_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if fd is not None:
            os.close(fd)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return SafeArtifact(str(path), len(data), hashlib.sha256(data).hexdigest())


def write_new_artifact(path: Path, data: bytes) -> SafeArtifact:
    return _atomic_write(path, data)


def _terminate_group(process: subprocess.Popen[bytes]) -> bool:
    try:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=2)
        return process.poll() is not None
    except (ProcessLookupError, PermissionError, subprocess.TimeoutExpired):
        return process.poll() is not None


def run_command(
    spec: CommandSpec,
    *,
    sensitive: SensitiveSet,
    callback: Callable[[str, str], T] | None = None,
) -> tuple[SafeCommandResult, T | None]:
    _validate_spec(spec, sensitive)
    env = _environment(spec.profile)
    started = _timestamp()
    sanitized_argv: list[str] = []
    for arg in spec.argv:
        cleaned = sanitize_value(arg, sensitive=sensitive, max_utf8_bytes=MAX_ARG_BYTES)
        if cleaned.value is None:
            raise SafeOutputError("ARGV_REDACTION_FAILED")
        sanitized_argv.append(cleaned.value)
    argv_descriptor = json.dumps(
        sanitized_argv, ensure_ascii=False, separators=(",", ":")
    ).encode()
    argv_sha = hashlib.sha256(argv_descriptor).hexdigest()
    try:
        process = subprocess.Popen(
            list(spec.argv),
            shell=False,
            cwd=spec.cwd,
            env=env,
            stdin=subprocess.PIPE
            if spec.stdin_bytes is not None
            else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            close_fds=True,
            start_new_session=True,
        )
    except OSError:
        result = SafeCommandResult(
            "beads.safe-command-result.v1",
            spec.profile,
            "SPAWN_FAILED",
            None,
            None,
            0,
            None,
            None,
            started,
            _timestamp(),
            "SPAWN_FAILED",
            argv_sha,
            0,
            0,
        )
        return result, None
    assert process.stdout is not None and process.stderr is not None
    buffers = [bytearray(), bytearray()]
    lock = threading.Lock()
    exceeded = threading.Event()
    read_failed = threading.Event()

    def reader(index: int, stream: Any, limit: int) -> None:
        try:
            while chunk := stream.read(READ_CHUNK_BYTES):
                with lock:
                    if (
                        len(buffers[index]) + len(chunk) > limit
                        or sum(map(len, buffers)) + len(chunk) > spec.total_limit_bytes
                    ):
                        exceeded.set()
                    elif not exceeded.is_set():
                        buffers[index].extend(chunk)
                if exceeded.is_set():
                    _terminate_group(process)
        except OSError:
            read_failed.set()
            _terminate_group(process)

    threads = [
        threading.Thread(
            target=reader,
            args=(0, process.stdout, spec.stdout_limit_bytes),
            daemon=True,
        ),
        threading.Thread(
            target=reader,
            args=(1, process.stderr, spec.stderr_limit_bytes),
            daemon=True,
        ),
    ]
    for thread in threads:
        thread.start()
    if spec.stdin_bytes is not None and process.stdin is not None:
        try:
            process.stdin.write(spec.stdin_bytes)
            process.stdin.close()
        except BrokenPipeError:
            pass
    deadline = time.monotonic() + spec.timeout_seconds
    timed_out = False
    while process.poll() is None and not exceeded.is_set() and not read_failed.is_set():
        if time.monotonic() >= deadline:
            timed_out = True
            _terminate_group(process)
            break
        time.sleep(0.01)
    if exceeded.is_set() or read_failed.is_set():
        _terminate_group(process)
    for thread in threads:
        thread.join(timeout=2)
    terminated = process.poll() is not None and all(
        not thread.is_alive() for thread in threads
    )
    finished = _timestamp()
    exit_code = process.poll()

    def make_result(
        status: str,
        error_code: str | None,
        *,
        redaction_count: int = 0,
        stdout_log: SafeArtifact | None = None,
        stderr_log: SafeArtifact | None = None,
        stdout_bytes: int | None = None,
        stderr_bytes: int | None = None,
    ) -> SafeCommandResult:
        return SafeCommandResult(
            "beads.safe-command-result.v1",
            spec.profile,
            status,
            exit_code,
            -exit_code if exit_code is not None and exit_code < 0 else None,
            redaction_count,
            stdout_log,
            stderr_log,
            started,
            finished,
            error_code,
            argv_sha,
            stdout_bytes,
            stderr_bytes,
        )

    if not terminated:
        return make_result("TERMINATION_UNKNOWN", "TERMINATION_UNKNOWN"), None
    if timed_out:
        return make_result("TIMED_OUT", "TIMED_OUT"), None
    if exceeded.is_set():
        return make_result("OUTPUT_LIMIT", "OUTPUT_LIMIT"), None
    if read_failed.is_set():
        return make_result("OUTPUT_POLICY_FAILED", "PIPE_READ_FAILED"), None
    try:
        texts = [bytes(buf).decode("utf-8", "strict") for buf in buffers]
    except UnicodeDecodeError:
        return make_result("INVALID_ENCODING", "INVALID_ENCODING"), None
    sanitized = [
        sanitize_value(text, sensitive=sensitive, max_utf8_bytes=limit)
        for text, limit in zip(
            texts, (spec.stdout_limit_bytes, spec.stderr_limit_bytes)
        )
    ]
    if any(item.value is None for item in sanitized):
        return make_result("REDACTION_FAILED", "REDACTION_FAILED"), None
    clean = [item.value or "" for item in sanitized]
    count = sum(item.redaction_count for item in sanitized)
    if spec.output_codec == "utf8_json":
        try:
            for text in clean:
                if text:
                    json.loads(text)
        except json.JSONDecodeError:
            return make_result("OUTPUT_POLICY_FAILED", "INVALID_JSON_OUTPUT"), None
    if spec.output_codec == "discard":
        clean = ["", ""]
    parsed: T | None = None
    if callback is not None:
        try:
            parsed = callback(clean[0], clean[1])
        except Exception:
            return make_result("OUTPUT_POLICY_FAILED", "CALLBACK_FAILED"), None
    out_art = (
        _atomic_write(spec.stdout_log, clean[0].encode())
        if spec.stdout_log is not None
        else None
    )
    err_art = (
        _atomic_write(spec.stderr_log, clean[1].encode())
        if spec.stderr_log is not None
        else None
    )
    status = "SUCCESS" if exit_code == 0 else "NONZERO_EXIT"
    return make_result(
        status,
        None if status == "SUCCESS" else "NONZERO_EXIT",
        redaction_count=count,
        stdout_log=out_art,
        stderr_log=err_art,
        stdout_bytes=len(clean[0].encode()),
        stderr_bytes=len(clean[1].encode()),
    ), parsed


def result_dict(result: SafeCommandResult) -> dict[str, Any]:
    value = asdict(result)
    schema_runtime.require_valid("safe-command-result-v1.schema.json", value)
    return value


def _under(path: Path, root: Path) -> bool:
    try:
        return bool(path.relative_to(root).parts)
    except ValueError:
        return False


def _artifact_destination(path: Path, root: Path) -> Path:
    if not path.is_absolute() or path.exists() or path.is_symlink():
        raise SafeOutputError("UNSAFE_ARTIFACT_PATH")
    try:
        parent = path.parent.resolve(strict=True)
    except OSError as exc:
        raise SafeOutputError("UNSAFE_ARTIFACT_PATH") from exc
    candidate = parent / path.name
    if candidate != path or not _under(candidate, root):
        raise SafeOutputError("UNSAFE_ARTIFACT_PATH")
    return candidate


def _load_sensitive_values(path: Path) -> SensitiveSet:
    if not path.is_absolute() or path.is_symlink():
        raise SafeOutputError("SENSITIVE_DESCRIPTOR_INVALID")
    try:
        if path.resolve(strict=True) != path or not path.is_file():
            raise SafeOutputError("SENSITIVE_DESCRIPTOR_INVALID")
        metadata = path.stat(follow_symlinks=False)
    except OSError:
        raise SafeOutputError("SENSITIVE_DESCRIPTOR_INVALID") from None
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise SafeOutputError("SENSITIVE_DESCRIPTOR_INVALID")
    value = schema_runtime.strict_json_loads(
        schema_runtime.read_bounded(path, 65536), max_bytes=65536
    )
    if not isinstance(value, list) or not all(
        isinstance(item, str) and len(item.encode("utf-8")) >= 8 for item in value
    ):
        raise SafeOutputError("SENSITIVE_DESCRIPTOR_INVALID")
    return SensitiveSet(tuple(value))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run-command")
    run.add_argument("--request", type=Path, required=True)
    run.add_argument("--result", type=Path, required=True)
    run.add_argument("--worker-packet", type=Path, required=True)
    run.add_argument("--expected-packet-sha256", required=True)
    run.add_argument("--sensitive-values-file", type=Path, required=True)
    sanitize = sub.add_parser("sanitize-value")
    sanitize.add_argument("--max-bytes", type=int, required=True)
    sanitize.add_argument("--sensitive-values-file", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        sensitive = _load_sensitive_values(args.sensitive_values_file)
        if args.command == "sanitize-value":
            raw = sys.stdin.buffer.read(args.max_bytes + 1)
            if len(raw) > args.max_bytes:
                raise SafeOutputError("INPUT_TOO_LARGE")
            try:
                text = raw.decode("utf-8", "strict")
            except UnicodeDecodeError as exc:
                raise SafeOutputError("INVALID_ENCODING") from exc
            sanitized = sanitize_value(
                text, sensitive=sensitive, max_utf8_bytes=args.max_bytes
            )
            payload = {
                "status": sanitized.status,
                "value": sanitized.value,
                "redaction_count": sanitized.redaction_count,
                "error_code": sanitized.error_code,
            }
            print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
            return 0 if sanitized.value is not None else 1
        raw = schema_runtime.read_bounded(args.request, 65536)
        value = schema_runtime.strict_json_loads(raw, max_bytes=65536)
        import validate_worker_packet

        packet = validate_worker_packet.validate_packet(
            schema_runtime.read_bounded(
                args.worker_packet, validate_worker_packet.MAX_PACKET_BYTES
            ),
            expected_packet_sha256=args.expected_packet_sha256,
        )
        expected = {
            "profile",
            "argv",
            "cwd",
            "stdin",
            "timeout_seconds",
            "stdout_limit_bytes",
            "stderr_limit_bytes",
            "total_limit_bytes",
            "output_codec",
            "stdout_log",
            "stderr_log",
            "artifact_root",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise SafeOutputError("INVALID_REQUEST")
        command_argv = tuple(value["argv"])
        if command_argv not in packet.required_commands:
            raise SafeOutputError("PACKET_COMMAND_MISMATCH")
        if Path(value["cwd"]) != Path(packet.value["repository"]["worktree"]):
            raise SafeOutputError("PACKET_CWD_MISMATCH")
        if Path(value["artifact_root"]) != Path(
            packet.value["verification"]["worker_outbox"]
        ):
            raise SafeOutputError("PACKET_ARTIFACT_ROOT_MISMATCH")
        declared_root = Path(value["artifact_root"])
        root = declared_root.resolve(strict=True)
        if (
            not declared_root.is_absolute()
            or declared_root.is_symlink()
            or root != declared_root
        ):
            raise SafeOutputError("UNSAFE_ARTIFACT_ROOT")
        result_path = _artifact_destination(args.result, root)
        stdout_log = (
            None
            if value["stdout_log"] is None
            else _artifact_destination(Path(value["stdout_log"]), root)
        )
        stderr_log = (
            None
            if value["stderr_log"] is None
            else _artifact_destination(Path(value["stderr_log"]), root)
        )
        destinations = [path for path in (result_path, stdout_log, stderr_log) if path]
        if len(destinations) != len(set(destinations)):
            raise SafeOutputError("ARTIFACT_PATH_ALIAS")
        spec = CommandSpec(
            value["profile"],
            command_argv,
            Path(value["cwd"]),
            None if value["stdin"] is None else value["stdin"].encode("utf-8"),
            value["timeout_seconds"],
            value["stdout_limit_bytes"],
            value["stderr_limit_bytes"],
            value["total_limit_bytes"],
            value["output_codec"],
            stdout_log,
            stderr_log,
        )
        result, _ = run_command(spec, sensitive=sensitive)
        payload = (
            json.dumps(
                result_dict(result), sort_keys=True, separators=(",", ":")
            ).encode()
            + b"\n"
        )
        _atomic_write(result_path, payload)
        print(
            json.dumps(
                {"status": result.status, "result": str(result_path)},
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 0 if result.status == "SUCCESS" else 1
    except (OSError, ValueError, TypeError):
        print('{"status":"REFUSED","error_code":"SAFE_OUTPUT_REFUSED"}')
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
