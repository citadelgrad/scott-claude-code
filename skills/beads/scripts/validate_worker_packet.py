#!/usr/bin/env python3
"""Strict worker-packet loading, semantic validation, and argv authorization."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Sequence

sys.path.insert(0, str(Path(__file__).parent))
import schema_runtime

MAX_PACKET_BYTES = 65536
_SHELLS = {
    "sh",
    "bash",
    "zsh",
    "fish",
    "cmd",
    "cmd.exe",
    "powershell",
    "powershell.exe",
    "pwsh",
    "pwsh.exe",
}
_INTERPRETERS = {"python", "python3", "python.exe", "node", "node.exe", "ruby", "perl"}
_COMMAND_WRAPPERS = {"command", "env", "sudo", "xargs"}
_REMOTE_COMMANDS = {"curl", "rsync", "scp", "ssh", "wget"}
_READONLY_GIT_COMMANDS = {
    "diff",
    "log",
    "ls-files",
    "rev-parse",
    "show",
    "status",
}
_PORTABLE_PATH = re.compile(r"^[\x20-\x7e]+$")
_GIT_GLOBAL_PATH_OVERRIDES = ("-C", "--git-dir", "--work-tree")


def _git_global_path_override(argv: Sequence[str]) -> bool:
    """Reject global Git directory/work-tree overrides, position-aware.

    Execution is bound to the packet's verified worktree (process cwd) and
    its common Git directory.  ``-C``/``--git-dir``/``--work-tree`` (including
    ``=`` and joined forms) are rejected **before the subcommand token**,
    where they relocate execution.  ``--git-dir``/``--work-tree`` remain
    rejected anywhere.  A bare ``-C <value>`` **after the ``commit``
    subcommand** is allowed: it reuses an existing commit's message
    (``git commit -C <commit>``) without relocating execution.  Joined
    ``-C<value>`` forms are rejected everywhere.
    """
    tail = list(argv[1:])
    subcommand = next((arg for arg in tail if not arg.startswith("-")), None)
    subcommand_index = tail.index(subcommand) if subcommand is not None else len(tail)
    for index, arg in enumerate(tail):
        after_subcommand = index > subcommand_index
        if arg == "--git-dir" or arg.startswith("--git-dir="):
            return True
        if arg == "--work-tree" or arg.startswith("--work-tree="):
            return True
        if arg == "-C":
            # Message reuse is only valid after the commit subcommand.
            if not (after_subcommand and subcommand == "commit"):
                return True
        elif arg.startswith("-C"):
            return True
    return False


def _bound_common_git_dir(worktree: Path, repository: Path) -> None:
    """Fail unless the worktree's resolved common Git directory is the
    repository root's own ``.git`` directory."""
    dot_git = worktree / ".git"
    if dot_git.is_symlink():
        _fail("WORKTREE_NOT_GIT")
    if dot_git.is_dir():
        common = dot_git.resolve(strict=True)
    elif dot_git.is_file():
        try:
            marker = dot_git.read_text(encoding="utf-8")
            if not marker.startswith("gitdir: ") or not marker.endswith("\n"):
                raise ValueError
            gitdir = Path(marker[len("gitdir: ") : -1])
            if not gitdir.is_absolute():
                gitdir = dot_git.parent / gitdir
            gitdir = gitdir.resolve(strict=True)
            if gitdir.parent.name != "worktrees" or not gitdir.is_dir():
                raise ValueError
            commondir_text = (gitdir / "commondir").read_text(encoding="utf-8").strip()
            if not commondir_text:
                raise ValueError
            common_path = Path(commondir_text)
            if not common_path.is_absolute():
                common_path = gitdir / common_path
            common = common_path.resolve(strict=True)
            backlink = (gitdir / "gitdir").read_text(encoding="utf-8").strip()
            if Path(backlink).resolve(strict=True) != dot_git.resolve(strict=True):
                raise ValueError
        except (OSError, ValueError, UnicodeError):
            _fail("WORKTREE_NOT_GIT")
    else:
        _fail("WORKTREE_NOT_GIT")
    root_git = repository / ".git"
    if not root_git.is_dir() or root_git.is_symlink():
        _fail("WORKTREE_NOT_GIT")
    if common != root_git.resolve(strict=True):
        _fail("WORKTREE_NOT_GIT")


class PacketValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ValidatedWorkerPacket:
    value: dict[str, Any]
    packet_sha256: str
    required_commands: tuple[tuple[str, ...], ...]
    allowed_issue_ids: frozenset[str]


def _fail(code: str) -> None:
    raise PacketValidationError(code)


def _canonical_directory(raw: str, code: str) -> Path:
    path = Path(raw)
    if not path.is_absolute() or not path.is_dir() or path.is_symlink():
        _fail(code)
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        _fail(code)
    if resolved != path:
        _fail(code)
    return resolved


def _strict_descendant(child: Path, parent: Path) -> bool:
    try:
        relative = child.relative_to(parent)
    except ValueError:
        return False
    return bool(relative.parts)


def _portable_relative(value: str) -> bool:
    if (
        not _PORTABLE_PATH.fullmatch(value)
        or "\\" in value
        or value.startswith("/")
        or re.match(r"^[A-Za-z]:", value)
    ):
        return False
    parts = value.split("/")
    return all(part not in ("", ".", "..") for part in parts)


def _scope_covers(relative: str, pattern: str) -> bool:
    """True when an allowed_paths pattern covers a worktree-relative path."""
    if pattern.endswith("/**"):
        root = pattern[:-3].rstrip("/")
        return relative == root or relative.startswith(root + "/")
    return relative == pattern or PurePosixPath(relative).match(pattern)


def _protected_write_scope(value: str) -> bool:
    first = value.split("/", 1)[0]
    return (
        any(character in first for character in "*?[{")
        or first in {".beads", ".git"}
        or first.startswith(".env")
    )


def _executable_names(argv: Sequence[str]) -> tuple[str, str]:
    first = Path(argv[0]).name.casefold() if argv else ""
    try:
        resolved_name = (
            Path(argv[0]).resolve(strict=True).name.casefold()
            if Path(argv[0]).is_absolute()
            else first
        )
    except OSError:
        resolved_name = first
    return first, resolved_name


def _is_git_command(argv: Sequence[str]) -> bool:
    if not argv:
        return False
    first, resolved_name = _executable_names(argv)
    return first == "git" or resolved_name == "git"


def _command_forbidden(
    argv: Sequence[str], *, allow_local_commit: bool = False
) -> bool:
    if not argv:
        return True
    first, resolved_name = _executable_names(argv)
    names = [Path(arg).name.casefold() for arg in argv]
    forbidden_executables = (
        {"bd", "beads"} | _SHELLS | _COMMAND_WRAPPERS | _REMOTE_COMMANDS
    )
    if (
        any(name in {"bd", "beads"} | _SHELLS | _COMMAND_WRAPPERS for name in names)
        or first in _REMOTE_COMMANDS
        or resolved_name in forbidden_executables
    ):
        return True
    if (
        first in _INTERPRETERS
        or resolved_name in _INTERPRETERS
        or first.startswith("python")
    ):
        if any(arg in ("-c", "-m") for arg in argv[1:]):
            return True
    if first == "git" or resolved_name == "git":
        if _git_global_path_override(argv):
            return True
        subcommand = next((arg for arg in argv[1:] if not arg.startswith("-")), None)
        allowed = set(_READONLY_GIT_COMMANDS)
        if allow_local_commit:
            allowed.update({"add", "commit"})
        return subcommand not in allowed
    if first == "uv" or resolved_name == "uv":
        if len(argv) < 2 or argv[1] != "run":
            return True
        for index, name in enumerate(names[2:], start=2):
            if name in _INTERPRETERS or name.startswith("python"):
                return any(arg in {"-c", "-m"} for arg in argv[index + 1 :])
    return False


def validate_packet(
    packet_bytes: bytes, *, expected_packet_sha256: str | None = None
) -> ValidatedWorkerPacket:
    if len(packet_bytes) > MAX_PACKET_BYTES:
        _fail("PACKET_TOO_LARGE")
    digest = hashlib.sha256(packet_bytes).hexdigest()
    if expected_packet_sha256 is not None and digest != expected_packet_sha256:
        _fail("PACKET_HASH_MISMATCH")
    try:
        value = schema_runtime.strict_json_loads(
            packet_bytes, max_bytes=MAX_PACKET_BYTES
        )
    except schema_runtime.JsonLoadFailure as exc:
        raise PacketValidationError(str(exc)) from None
    if not isinstance(value, dict):
        _fail("PACKET_NOT_OBJECT")
    findings = schema_runtime.validate_instance("worker-packet-v1.schema.json", value)
    if findings:
        _fail(f"PACKET_SCHEMA_INVALID:{findings[0].code}:{findings[0].instance_path}")
    issue_id = value["issue"]["id"]
    if not 1 <= len(issue_id.encode("utf-8")) <= 1024:
        _fail("ISSUE_ID_BYTES")
    if hashlib.sha256(issue_id.encode("utf-8")).hexdigest() != value["issue"]["key"]:
        _fail("ISSUE_KEY_MISMATCH")
    repository = _canonical_directory(value["repository"]["root"], "REPOSITORY_INVALID")
    worktree = _canonical_directory(value["repository"]["worktree"], "WORKTREE_INVALID")
    outbox = _canonical_directory(
        value["verification"]["worker_outbox"], "OUTBOX_INVALID"
    )
    import_root = _canonical_directory(
        value["verification"]["parent_import_root"], "IMPORT_ROOT_INVALID"
    )
    if not _strict_descendant(worktree, repository) or not _strict_descendant(
        outbox, worktree
    ):
        _fail("PATH_CONTAINMENT_INVALID")
    if (
        outbox == import_root
        or _strict_descendant(outbox, import_root)
        or _strict_descendant(import_root, outbox)
    ):
        _fail("PATH_ALIAS_INVALID")
    result_schema = Path(value["return_contract"]["schema"])
    expected_result_schema = (
        Path(__file__).parents[1] / "schemas" / "worker-execution-result-v1.schema.json"
    ).resolve(strict=True)
    try:
        canonical_result_schema = result_schema.resolve(strict=True)
    except OSError:
        _fail("RESULT_SCHEMA_MISMATCH")
    if (
        result_schema != canonical_result_schema
        or result_schema != expected_result_schema
    ):
        _fail("RESULT_SCHEMA_MISMATCH")
    for field in ("allowed_paths", "forbidden_paths"):
        if not all(_portable_relative(item) for item in value["scope"][field]):
            _fail("SCOPE_PATH_INVALID")
    if any(_protected_write_scope(item) for item in value["scope"]["allowed_paths"]):
        _fail("SCOPE_PROTECTED")
    # A worker outbox inside allowed_paths would silently exclude its
    # deliverables from every downstream inventory/freeze/review scan.
    try:
        outbox_relative = outbox.relative_to(worktree).as_posix()
    except ValueError:  # pragma: no cover - containment checked above
        _fail("PATH_CONTAINMENT_INVALID")
    if any(
        _scope_covers(outbox_relative, item) for item in value["scope"]["allowed_paths"]
    ):
        _fail("OUTBOX_INSIDE_SCOPE")
    commands = tuple(tuple(item) for item in value["verification"]["required_commands"])
    for command in commands:
        if _command_forbidden(
            command, allow_local_commit=value["scope"]["local_commit"]
        ):
            _fail("FORBIDDEN_COMMAND")
        if sum(len(arg.encode("utf-8")) for arg in command) > 32768 or any(
            "\x00" in arg for arg in command
        ):
            _fail("COMMAND_INVALID")
        if _is_git_command(command):
            _bound_common_git_dir(worktree, repository)
    allowed = frozenset([issue_id, *value["prerequisites"]["issue_ids"]])
    return ValidatedWorkerPacket(value, digest, commands, allowed)


def authorize_command(packet: ValidatedWorkerPacket, argv: Sequence[str]) -> int:
    candidate = tuple(argv)
    if _command_forbidden(
        candidate, allow_local_commit=packet.value["scope"]["local_commit"]
    ):
        _fail("FORBIDDEN_COMMAND")
    try:
        return packet.required_commands.index(candidate)
    except ValueError:
        _fail("COMMAND_NOT_DECLARED")
        raise AssertionError("unreachable")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate")
    validate.add_argument("--packet", type=Path, required=True)
    validate.add_argument("--expected-sha256")
    validate.add_argument("--json", action="store_true")
    check = sub.add_parser("check-command")
    check.add_argument("--packet", type=Path, required=True)
    check.add_argument("--argv-file", type=Path, required=True)
    check.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        packet = validate_packet(
            schema_runtime.read_bounded(args.packet, MAX_PACKET_BYTES),
            expected_packet_sha256=getattr(args, "expected_sha256", None),
        )
        result: dict[str, Any] = {
            "status": "valid",
            "packet_sha256": packet.packet_sha256,
        }
        if args.command == "check-command":
            command = schema_runtime.strict_json_loads(
                schema_runtime.read_bounded(args.argv_file, 65536), max_bytes=65536
            )
            if not isinstance(command, list) or not all(
                isinstance(x, str) for x in command
            ):
                _fail("ARGV_INVALID")
            result["command_index"] = authorize_command(packet, command)
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    except (OSError, PacketValidationError, schema_runtime.JsonLoadFailure):
        print('{"status":"invalid","error_code":"WORKER_PACKET_INVALID"}')
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
