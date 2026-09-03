from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
MODULE = ROOT / "skills/beads/scripts/safe_output.py"


def _load():
    spec = importlib.util.spec_from_file_location("beads_safe_output", MODULE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _packet_factory_module():
    path = Path(__file__).with_name("test_worker_packet.py")
    spec = importlib.util.spec_from_file_location("test_worker_packet_factory", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_run_command_redacts_before_owner_mode_persistence(tmp_path: Path) -> None:
    safe = _load()
    os.chmod(tmp_path, 0o700)
    sentinel = "synthetic-secret-value-0123456789"
    stdout_log = tmp_path / "stdout.log"
    stderr_log = tmp_path / "stderr.log"
    code = "import sys; print(sys.argv[1]); print('ok', file=sys.stderr)"
    spec = safe.CommandSpec(
        profile="hermetic",
        argv=(sys.executable, "-c", code, sentinel),
        cwd=tmp_path,
        stdin_bytes=None,
        timeout_seconds=10,
        stdout_limit_bytes=1024,
        stderr_limit_bytes=1024,
        total_limit_bytes=2048,
        output_codec="utf8_text",
        stdout_log=stdout_log,
        stderr_log=stderr_log,
    )
    result, captured = safe.run_command(
        spec,
        sensitive=safe.SensitiveSet((sentinel,)),
        callback=lambda out, err: (out, err),
    )
    assert result.status == "SUCCESS"
    assert captured == ("[REDACTED]\n", "ok\n")
    assert sentinel.encode() not in stdout_log.read_bytes()
    assert stat.S_IMODE(stdout_log.stat().st_mode) == 0o600


def test_ambiguous_or_oversized_output_writes_no_log(tmp_path: Path) -> None:
    safe = _load()
    os.chmod(tmp_path, 0o700)
    log = tmp_path / "unsafe.log"
    ambiguous = safe.sanitize_value(
        "ｔｏｋｅｎ=abcdef0123456789",
        sensitive=safe.SensitiveSet(()),
        max_utf8_bytes=100,
    )
    assert ambiguous.status == "REDACTION_FAILED"
    spec = safe.CommandSpec(
        profile="hermetic",
        argv=(sys.executable, "-c", "print('x'*50)"),
        cwd=tmp_path,
        stdin_bytes=None,
        timeout_seconds=10,
        stdout_limit_bytes=10,
        stderr_limit_bytes=10,
        total_limit_bytes=20,
        output_codec="utf8_text",
        stdout_log=log,
        stderr_log=None,
    )
    result, captured = safe.run_command(spec, sensitive=safe.SensitiveSet(()))
    assert result.status == "OUTPUT_LIMIT"
    assert captured is None and not log.exists()


def test_popen_is_no_shell_and_environment_is_allowlisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    safe = _load()
    os.chmod(tmp_path, 0o700)
    calls: list[dict] = []
    original = safe.subprocess.Popen

    def spy(*args, **kwargs):
        calls.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(safe.subprocess, "Popen", spy)
    monkeypatch.setenv("SYNTHETIC_API_TOKEN", "must-not-inherit-0123456789")
    spec = safe.CommandSpec(
        profile="hermetic",
        argv=(sys.executable, "-c", "print('ok')"),
        cwd=tmp_path,
        stdin_bytes=None,
        timeout_seconds=10,
        stdout_limit_bytes=100,
        stderr_limit_bytes=100,
        total_limit_bytes=200,
        output_codec="utf8_text",
        stdout_log=None,
        stderr_log=None,
    )
    result, _ = safe.run_command(spec, sensitive=safe.SensitiveSet(()))
    assert result.status == "SUCCESS"
    assert calls[0]["shell"] is False
    assert "SYNTHETIC_API_TOKEN" not in calls[0]["env"]
    assert calls[0]["start_new_session"] is True


def test_short_sensitive_value_is_refused_before_spawn(tmp_path: Path) -> None:
    safe = _load()
    os.chmod(tmp_path, 0o700)
    spec = safe.CommandSpec(
        profile="hermetic",
        argv=(sys.executable, "-c", "print('never')"),
        cwd=tmp_path,
        stdin_bytes=None,
        timeout_seconds=10,
        stdout_limit_bytes=100,
        stderr_limit_bytes=100,
        total_limit_bytes=200,
        output_codec="utf8_text",
        stdout_log=None,
        stderr_log=None,
    )
    with pytest.raises(safe.SafeOutputError, match="SENSITIVE_VALUE_AMBIGUOUS"):
        safe.run_command(spec, sensitive=safe.SensitiveSet(("abc",)))


def test_stdin_backpressure_cannot_bypass_timeout(tmp_path: Path) -> None:
    safe = _load()
    os.chmod(tmp_path, 0o700)
    spec = safe.CommandSpec(
        profile="hermetic",
        argv=(sys.executable, "-c", "import time; time.sleep(10)"),
        cwd=tmp_path,
        stdin_bytes=b"x" * safe.MAX_STDIN_BYTES,
        timeout_seconds=1,
        stdout_limit_bytes=100,
        stderr_limit_bytes=100,
        total_limit_bytes=200,
        output_codec="utf8_text",
        stdout_log=None,
        stderr_log=None,
    )

    started = time.monotonic()
    result, captured = safe.run_command(spec, sensitive=safe.SensitiveSet(()))

    assert time.monotonic() - started < 5
    assert result.status == "TIMED_OUT"
    assert captured is None


def test_cli_rejects_log_paths_outside_artifact_root(tmp_path: Path) -> None:
    safe = _load()
    artifact_root = tmp_path / "artifacts"
    outside = tmp_path / "outside"
    artifact_root.mkdir(mode=0o700)
    outside.mkdir(mode=0o700)
    escaped_log = outside / "stdout.log"

    with pytest.raises(safe.SafeOutputError, match="UNSAFE_ARTIFACT_PATH"):
        safe._artifact_destination(escaped_log, artifact_root)

    assert not escaped_log.exists()


def test_atomic_artifact_write_never_publishes_partial_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    safe = _load()
    os.chmod(tmp_path, 0o700)
    destination = tmp_path / "result.json"
    real_write = safe.os.write
    calls = 0

    def short_then_fail(fd: int, data: bytes) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            return real_write(fd, data[:1])
        raise OSError("injected write failure")

    monkeypatch.setattr(safe.os, "write", short_then_fail)

    with pytest.raises(OSError, match="injected write failure"):
        safe.write_new_artifact(destination, b'{"complete":true}\n')

    assert not destination.exists()
    assert not list(tmp_path.glob(".result.json.tmp-*"))


def test_artifact_write_rejects_symlink_ancestor(tmp_path: Path) -> None:
    safe = _load()
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    nested = outside / "nested"
    root.mkdir(mode=0o700)
    nested.mkdir(parents=True, mode=0o700)
    (root / "link").symlink_to(outside, target_is_directory=True)
    destination = root / "link" / "nested" / "result.json"

    with pytest.raises(safe.SafeOutputError, match="UNSAFE_LOG_PATH"):
        safe.write_new_artifact(destination, b"complete\n")

    assert not (nested / "result.json").exists()


def test_command_rejects_cwd_with_symlink_ancestor(tmp_path: Path) -> None:
    safe = _load()
    real = tmp_path / "real"
    nested = real / "nested"
    nested.mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    spec = safe.CommandSpec(
        profile="hermetic",
        argv=(sys.executable, "-c", "print('must not run')"),
        cwd=link / "nested",
        stdin_bytes=None,
        timeout_seconds=5,
        stdout_limit_bytes=100,
        stderr_limit_bytes=100,
        total_limit_bytes=200,
        output_codec="utf8_text",
        stdout_log=None,
        stderr_log=None,
    )

    with pytest.raises(safe.SafeOutputError, match="INVALID_CWD"):
        safe.run_command(spec, sensitive=safe.SensitiveSet(()))


def test_cli_requires_exact_packet_bound_command(tmp_path: Path) -> None:
    safe = _load()
    packet_factory = _packet_factory_module()
    packet = packet_factory._packet(tmp_path)
    sentinel = "redaction-probe-value-9876543210"
    command = ["/usr/bin/printf", "%s", sentinel]
    packet["verification"]["required_commands"] = [command]
    packet_path = tmp_path / "packet.json"
    packet_path.write_text(json.dumps(packet))
    packet_sha = hashlib.sha256(packet_path.read_bytes()).hexdigest()
    packet_factory._load().validate_packet(
        packet_path.read_bytes(), expected_packet_sha256=packet_sha
    )
    outbox = Path(packet["verification"]["worker_outbox"])
    sensitive_values = tmp_path / "sensitive-values.json"
    sensitive_values.write_text(json.dumps([sentinel]))
    sensitive_values.chmod(0o600)

    def request(argv: list[str], name: str) -> Path:
        path = tmp_path / f"{name}.request.json"
        path.write_text(
            json.dumps(
                {
                    "profile": "hermetic",
                    "argv": argv,
                    "cwd": packet["repository"]["worktree"],
                    "stdin": None,
                    "timeout_seconds": 10,
                    "stdout_limit_bytes": 4096,
                    "stderr_limit_bytes": 4096,
                    "total_limit_bytes": 8192,
                    "output_codec": "utf8_text",
                    "stdout_log": str(outbox / f"{name}.stdout"),
                    "stderr_log": None,
                    "artifact_root": str(outbox),
                }
            )
        )
        return path

    result = outbox / "result.json"
    assert (
        safe.main(
            [
                "run-command",
                "--request",
                str(request(command, "allowed")),
                "--result",
                str(result),
                "--worker-packet",
                str(packet_path),
                "--expected-packet-sha256",
                packet_sha,
                "--sensitive-values-file",
                str(sensitive_values),
            ]
        )
        == 0
    )
    assert result.is_file()
    assert sentinel not in result.read_text()
    assert (outbox / "allowed.stdout").read_text() == "[REDACTED]"

    denied_result = outbox / "denied.json"
    assert (
        safe.main(
            [
                "run-command",
                "--request",
                str(request(["/usr/bin/false"], "denied")),
                "--result",
                str(denied_result),
                "--worker-packet",
                str(packet_path),
                "--expected-packet-sha256",
                packet_sha,
                "--sensitive-values-file",
                str(sensitive_values),
            ]
        )
        == 2
    )
    assert not denied_result.exists()
