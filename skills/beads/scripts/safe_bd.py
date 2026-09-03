#!/usr/bin/env python3
"""Closed, pinned Beads 1.2.2 transport built on safe_output."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

sys.path.insert(0, str(Path(__file__).parent))
import safe_output
import schema_runtime

PINNED_BD_VERSION = "1.2.2"
CLI_CONTRACT_HASHES = {
    "bd prime": "993713b7b4f06101731ee8e8efb3f0e692858c273525a91ae4bb8b3b053384ba",
    "bd create --help": "d06e962277a4faaab8c8e5e9e59875bcca4a84aee26f03bfb47a42557e74ce74",
    "bd update --help": "562e3f92ccc173e40c0a9a83302cd32b0cd961e33412b192352f1205583f2130",
    "bd close --help": "ed1e73d5d8ff9714bdbeca9c57ab7cdd8eff66c5d0167fc3f76715be7faa2521",
    "bd dep --help": "ec0134ef777218dd5fefc958e30079acb2f0a96aa451a6d5a22124f3e77d1943",
    "bd ready --help": "7d485a5c4eb3505c1e2e4fa700ddbf80964a25c6fd047341a2ccfc9872b97393",
    "bd show --help": "010c34cfe1bf28beedf9c90978979a957e612ce8bd8a56c5b47e2cdde5038179",
    "bd human --help": "eef6aff710c947fb2e2ab297583d22049a2bc6c38715f5284a6ed4bf1666a61c",
    "bd gate --help": "16959572cdfc30b030828c6ae440f42933872fef4e876ae14603ce0f8d8e3bf0",
    "bd worktree --help": "a32cd089a3c55e14fa90b9a3785eb7c0db2786f4c47a5f99954cd63a677a7fa9",
}
PROFILE_CONTRACT_COMMANDS = {
    "issue_get": "bd show --help",
    "ready_list": "bd ready --help",
    "dependency_list": "bd dep --help",
    "dependency_cycles": "bd dep --help",
    "claim_exact": "bd update --help",
    "set_run_pointer": "bd update --help",
    "close_exact": "bd close --help",
    "create_exact": "bd create --help",
    "dependency_add_exact": "bd dep --help",
    "dependency_remove_exact": "bd dep --help",
    "gate_list": "bd gate --help",
    "gate_show": "bd gate --help",
    "gate_check": "bd gate --help",
    "gate_resolve": "bd gate --help",
    "human_list": "bd human --help",
}
MAX_NATIVE_BYTES = 2 * 1024 * 1024


class SafeBdError(ValueError):
    pass


@dataclass(frozen=True)
class WorkerScope:
    allowed_issue_ids: frozenset[str]


@dataclass(frozen=True)
class Profile:
    mutation: bool
    worker_allowed: bool
    arguments: frozenset[str]
    codec: str
    timeout: int = 30


PROFILES: dict[str, Profile] = {
    "prime": Profile(False, True, frozenset(), "text"),
    "version": Profile(False, True, frozenset(), "text"),
    "workspace_where": Profile(False, True, frozenset(), "json"),
    "workspace_status": Profile(False, True, frozenset(), "json"),
    "doctor": Profile(False, True, frozenset(), "text"),
    "issue_get": Profile(False, True, frozenset({"issue_id"}), "json"),
    "issue_comments": Profile(False, True, frozenset({"issue_id"}), "json"),
    "issue_history": Profile(False, True, frozenset({"issue_id", "limit"}), "json"),
    "children_list": Profile(False, False, frozenset({"issue_id"}), "json"),
    "issue_list": Profile(
        False, False, frozenset({"status", "parent", "limit"}), "json"
    ),
    "ready_list": Profile(False, False, frozenset({"parent", "limit"}), "json"),
    "blocked_list": Profile(False, False, frozenset({"parent"}), "json"),
    "dependency_list": Profile(False, False, frozenset({"issue_id"}), "json"),
    "dependency_cycles": Profile(False, False, frozenset(), "json"),
    "gate_list": Profile(False, False, frozenset(), "json"),
    "gate_show": Profile(False, False, frozenset({"gate_id"}), "json"),
    "human_list": Profile(False, False, frozenset(), "json"),
    "claim_exact": Profile(True, False, frozenset({"issue_id", "actor"}), "json"),
    "append_marker_note": Profile(
        True, False, frozenset({"issue_id", "actor", "content"}), "json"
    ),
    "close_exact": Profile(
        True, False, frozenset({"issue_id", "actor", "reason"}), "json"
    ),
    "create_exact": Profile(
        True,
        False,
        frozenset({"issue_id", "actor", "title", "issue_type", "priority"}),
        "json",
    ),
    "dependency_add_exact": Profile(
        True, False, frozenset({"dependent", "prerequisite", "actor"}), "json"
    ),
    "dependency_remove_exact": Profile(
        True, False, frozenset({"dependent", "prerequisite", "actor"}), "json"
    ),
    "set_run_pointer": Profile(
        True, False, frozenset({"issue_id", "actor", "value"}), "json"
    ),
    "gate_check": Profile(True, False, frozenset({"gate_id", "actor"}), "json"),
    "gate_resolve": Profile(
        True, False, frozenset({"gate_id", "actor", "reason"}), "json"
    ),
}
_ID_FIELDS = {"issue_id", "dependent", "prerequisite", "gate_id"}
_ISSUE_FIELDS = frozenset(
    {
        "id",
        "title",
        "description",
        "design",
        "acceptance_criteria",
        "notes",
        "status",
        "priority",
        "issue_type",
        "assignee",
        "owner",
        "parent",
        "labels",
        "dependencies",
        "dependents",
        "created_at",
        "created_by",
        "updated_at",
        "started_at",
        "closed_at",
        "close_reason",
        "metadata",
        "external_ref",
        "estimated_minutes",
        "comment_count",
        "dependency_count",
        "dependent_count",
        "spec_id",
        "blocked_by",
        "blocked_by_count",
        "dependency_type",
    }
)
_LIST_FIELDS = _ISSUE_FIELDS
_CYCLE_FIELDS = frozenset({"cycles", "count"})
_WORKSPACE_FIELDS = frozenset(
    {
        "workspace",
        "path",
        "repository_root",
        "database",
        "mode",
        "status",
        "healthy",
        "version",
        "dolt_server_port",
        "issues",
        "counts",
        "warnings",
        "schema_version",
        "database_path",
        "prefix",
        "summary",
    }
)
_EVENT_FIELDS = frozenset(
    {
        "id",
        "issue_id",
        "event_type",
        "actor",
        "timestamp",
        "created_at",
        "old_value",
        "new_value",
        "comment",
        "text",
        "reason",
        "metadata",
        "CommitDate",
        "CommitHash",
        "Committer",
        "Issue",
    }
)
_GATE_FIELDS = frozenset(
    {
        "id",
        "title",
        "status",
        "type",
        "target",
        "timeout",
        "created_at",
        "updated_at",
        "resolved_at",
        "reason",
    }
)


@dataclass(frozen=True)
class SafeBdRequest:
    profile: str
    arguments: Mapping[str, Any]
    repository_root: Path
    worker_packet: WorkerScope | None

    def __post_init__(self) -> None:
        profile = PROFILES.get(self.profile)
        if profile is None:
            raise SafeBdError("UNKNOWN_PROFILE")
        unknown = set(self.arguments) - profile.arguments
        missing = {
            x
            for x in profile.arguments
            if x not in self.arguments and x not in {"limit", "status", "parent"}
        }
        if unknown:
            raise SafeBdError("UNKNOWN_ARGUMENT")
        if missing:
            raise SafeBdError("MISSING_ARGUMENT")
        if not self.repository_root.is_absolute():
            raise SafeBdError("INVALID_REPOSITORY_ROOT")
        for key in _ID_FIELDS & self.arguments.keys():
            value = self.arguments[key]
            if not isinstance(value, str) or not value or "\x00" in value:
                raise SafeBdError("INVALID_TARGET")
        if self.worker_packet is not None:
            if profile.mutation:
                raise SafeBdError("WORKER_MUTATION_REFUSED")
            if not profile.worker_allowed:
                raise SafeBdError("WORKER_PROFILE_REFUSED")
            target = self.arguments.get("issue_id")
            if (
                target is not None
                and target not in self.worker_packet.allowed_issue_ids
            ):
                raise SafeBdError("WORKER_TARGET_REFUSED")


@dataclass(frozen=True)
class SafeBdResult:
    schema_version: str
    profile: str
    status: str
    cli_version: str
    workspace_sha256: str | None
    data: Any
    warnings: tuple[dict[str, Any], ...]
    error_code: str | None


def _integer(value: Any, name: str, low: int, high: int) -> str:
    if type(value) is not int or not low <= value <= high:
        raise SafeBdError(f"INVALID_{name.upper()}")
    return str(value)


def build_argv(request: SafeBdRequest, *, executable: Path) -> tuple[str, ...]:
    if not executable.is_absolute() or executable.name not in {"bd", "beads"}:
        raise SafeBdError("INVALID_EXECUTABLE")
    a = request.arguments
    prefix = [str(executable)]
    if PROFILES[request.profile].mutation:
        prefix += ["--sandbox", "--json"]
    elif request.profile not in {"version"}:
        prefix += ["--readonly"] + ([] if request.profile == "prime" else ["--json"])
    p = request.profile
    if p == "prime":
        return tuple(prefix + ["prime"])
    if p == "version":
        return tuple(prefix + ["version"])
    if p == "workspace_where":
        return tuple(prefix + ["where"])
    if p in {"workspace_status", "doctor"}:
        return tuple(
            prefix
            + (["status", "--no-activity"] if p == "workspace_status" else ["doctor"])
        )
    if p == "issue_get":
        return tuple(prefix + ["show", f"--id={a['issue_id']}"])
    if p == "issue_comments":
        return tuple(prefix + ["comments", "--", a["issue_id"]])
    if p == "issue_history":
        return tuple(
            prefix
            + [
                "history",
                f"--limit={_integer(a.get('limit', 100), 'limit', 1, 1000)}",
                "--",
                a["issue_id"],
            ]
        )
    if p == "children_list":
        return tuple(prefix + ["children", "--", a["issue_id"]])
    if p == "issue_list":
        args = prefix + [
            "list",
            f"--limit={_integer(a.get('limit', 100), 'limit', 1, 1000)}",
        ]
        if a.get("status") is not None:
            args.append(f"--status={a['status']}")
        if a.get("parent") is not None:
            args.append(f"--parent={a['parent']}")
        return tuple(args)
    if p in {"ready_list", "blocked_list"}:
        args = prefix + (
            ["ready", f"--limit={_integer(a.get('limit', 100), 'limit', 1, 1000)}"]
            if p == "ready_list"
            else ["blocked"]
        )
        if a.get("parent") is not None:
            args.append(f"--parent={a['parent']}")
        return tuple(args)
    if p == "dependency_list":
        return tuple(prefix + ["dep", "list", "--", a["issue_id"]])
    if p == "dependency_cycles":
        return tuple(prefix + ["dep", "cycles"])
    if p == "gate_list":
        return tuple(prefix + ["gate", "list"])
    if p == "gate_show":
        return tuple(prefix + ["gate", "show", "--", a["gate_id"]])
    if p == "human_list":
        return tuple(prefix + ["human", "list"])
    actor = [f"--actor={a['actor']}"]
    if p == "claim_exact":
        return tuple(prefix + actor + ["update", "--claim", "--", a["issue_id"]])
    if p == "append_marker_note":
        return tuple(prefix + actor + ["note", "--stdin", "--", a["issue_id"]])
    if p == "close_exact":
        return tuple(
            prefix + actor + ["close", f"--reason={a['reason']}", "--", a["issue_id"]]
        )
    if p == "create_exact":
        return tuple(
            prefix
            + actor
            + [
                "create",
                f"--id={a['issue_id']}",
                f"--title={a['title']}",
                f"--type={a['issue_type']}",
                f"--priority={_integer(a['priority'], 'priority', 0, 4)}",
            ]
        )
    if p in {"dependency_add_exact", "dependency_remove_exact"}:
        return tuple(
            prefix
            + actor
            + [
                "dep",
                "add" if p.endswith("add_exact") else "remove",
                "--",
                a["dependent"],
                a["prerequisite"],
            ]
        )
    if p == "set_run_pointer":
        return tuple(
            prefix
            + actor
            + [
                "update",
                "--set-metadata",
                f"hermes.beads_run.v1={a['value']}",
                "--",
                a["issue_id"],
            ]
        )
    if p == "gate_check":
        return tuple(prefix + actor + ["gate", "check", "--", a["gate_id"]])
    if p == "gate_resolve":
        return tuple(
            prefix
            + actor
            + ["gate", "resolve", f"--reason={a['reason']}", "--", a["gate_id"]]
        )
    raise SafeBdError("UNKNOWN_PROFILE")


def _allowed_fields(profile: str) -> frozenset[str]:
    if profile in {"workspace_where", "workspace_status"}:
        return _WORKSPACE_FIELDS
    if profile == "dependency_cycles":
        return _CYCLE_FIELDS
    if profile.startswith("gate_"):
        return _GATE_FIELDS
    if profile in {"issue_history", "issue_comments"}:
        return _EVENT_FIELDS
    return _LIST_FIELDS


def _require_fields(value: Mapping[str, Any], required: frozenset[str]) -> None:
    if not required.issubset(value):
        raise SafeBdError("BD_OUTPUT_DRIFT")


_ISSUE_STRING_FIELDS = frozenset(
    {
        "id",
        "title",
        "description",
        "design",
        "acceptance_criteria",
        "notes",
        "status",
        "issue_type",
        "assignee",
        "owner",
        "parent",
        "created_at",
        "created_by",
        "updated_at",
        "started_at",
        "closed_at",
        "close_reason",
        "external_ref",
        "spec_id",
        "dependency_type",
    }
)
_ISSUE_INTEGER_FIELDS = frozenset(
    {
        "priority",
        "estimated_minutes",
        "comment_count",
        "dependency_count",
        "dependent_count",
        "blocked_by_count",
    }
)
_STATUS_SUMMARY_FIELDS = frozenset(
    {
        "average_lead_time_hours",
        "blocked_issues",
        "closed_issues",
        "deferred_issues",
        "epics_eligible_for_closure",
        "in_progress_issues",
        "open_issues",
        "pinned_issues",
        "ready_issues",
        "total_issues",
    }
)
_DEPENDENCY_FIELDS = frozenset(
    {"issue_id", "depends_on_id", "type", "created_at", "created_by", "metadata"}
)


def _string(value: Any, *, nullable: bool = True) -> bool:
    return isinstance(value, str) or (nullable and value is None)


def _exact_integer(value: Any, *, nullable: bool = True) -> bool:
    return (type(value) is int and value >= 0) or (nullable and value is None)


def _validate_issue_record(value: Mapping[str, Any]) -> None:
    for key, item in value.items():
        if key in _ISSUE_STRING_FIELDS:
            valid = _string(item)
        elif key in _ISSUE_INTEGER_FIELDS:
            valid = _exact_integer(item)
            if key == "priority" and item is not None:
                valid = valid and item <= 4
        elif key in {"labels", "blocked_by"}:
            valid = isinstance(item, list) and all(
                isinstance(entry, str) for entry in item
            )
        elif key in {"dependencies", "dependents"}:
            valid = isinstance(item, list) and all(
                isinstance(entry, dict) for entry in item
            )
            if valid:
                for entry in item:
                    if not (set(entry) - _ISSUE_FIELDS) and isinstance(
                        entry.get("id"), str
                    ):
                        _validate_issue_record(entry)
                    elif not (set(entry) - _DEPENDENCY_FIELDS) and {
                        "issue_id",
                        "depends_on_id",
                        "type",
                    }.issubset(entry):
                        valid = all(
                            _string(entry.get(field), nullable=False)
                            for field in ("issue_id", "depends_on_id", "type")
                        ) and all(
                            _string(entry[field])
                            for field in ("created_at", "created_by", "metadata")
                            if field in entry
                        )
                    else:
                        valid = False
                    if not valid:
                        break
        elif key == "metadata":
            valid = item is None or isinstance(item, dict)
        else:
            valid = False
        if not valid:
            raise SafeBdError("BD_OUTPUT_DRIFT")


def _sanitize_tree(value: Any, *, sensitive: safe_output.SensitiveSet) -> Any:
    if isinstance(value, str):
        result = safe_output.sanitize_value(
            value, sensitive=sensitive, max_utf8_bytes=65536
        )
        if result.value is None:
            raise SafeBdError("BD_REDACTION_FAILED")
        return result.value
    if isinstance(value, list):
        return [_sanitize_tree(item, sensitive=sensitive) for item in value]
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            sanitized_key = safe_output.sanitize_value(
                key, sensitive=sensitive, max_utf8_bytes=65536
            )
            if sanitized_key.value is None or sanitized_key.value in clean:
                raise SafeBdError("BD_REDACTION_FAILED")
            clean[sanitized_key.value] = (
                "[REDACTED]"
                if safe_output.is_sensitive_label(key)
                else _sanitize_tree(item, sensitive=sensitive)
            )
        return clean
    return value


def decode_output(
    request: SafeBdRequest,
    stdout: str,
    *,
    sensitive: safe_output.SensitiveSet = safe_output.SensitiveSet(()),
) -> Any:
    profile = PROFILES[request.profile]
    if profile.codec == "text":
        result = safe_output.sanitize_value(
            stdout,
            sensitive=sensitive,
            max_utf8_bytes=MAX_NATIVE_BYTES,
        )
        if result.value is None:
            raise SafeBdError("BD_REDACTION_FAILED")
        if not result.value.strip():
            raise SafeBdError("BD_OUTPUT_DRIFT")
        return result.value
    try:
        value = schema_runtime.strict_json_loads(
            stdout.encode("utf-8"), max_bytes=MAX_NATIVE_BYTES
        )
    except (UnicodeError, schema_runtime.JsonLoadFailure):
        raise SafeBdError("BD_OUTPUT_MALFORMED") from None
    if value is None and request.profile == "gate_list":
        value = []
    if request.profile == "dependency_cycles" and isinstance(value, list):
        value = {"cycles": value, "count": len(value)}
    dict_profiles = {"workspace_where", "workspace_status", "dependency_cycles"}
    list_profiles = {
        "issue_get",
        "issue_comments",
        "issue_history",
        "children_list",
        "issue_list",
        "ready_list",
        "blocked_list",
        "dependency_list",
        "gate_list",
        "human_list",
    }
    if request.profile in dict_profiles and not isinstance(value, dict):
        raise SafeBdError("BD_OUTPUT_DRIFT")
    if request.profile in list_profiles and not isinstance(value, list):
        raise SafeBdError("BD_OUTPUT_DRIFT")
    records = value if isinstance(value, list) else [value]
    if not all(isinstance(item, dict) for item in records):
        raise SafeBdError("BD_OUTPUT_MALFORMED")
    allowed = _allowed_fields(request.profile)
    for item in records:
        if set(item) - allowed:
            raise SafeBdError("BD_OUTPUT_DRIFT")
    if request.profile == "issue_get":
        if len(records) != 1:
            raise SafeBdError("BD_OUTPUT_DRIFT")
        _require_fields(records[0], frozenset({"id", "title", "status"}))
        if records[0]["id"] != request.arguments["issue_id"]:
            raise SafeBdError("BD_OUTPUT_DRIFT")
        _validate_issue_record(records[0])
    elif request.profile == "workspace_where":
        _require_fields(records[0], frozenset({"path", "database_path"}))
        observed_path = records[0]["path"]
        database = records[0]["database_path"]
        if not isinstance(observed_path, str) or not isinstance(database, str):
            raise SafeBdError("BD_OUTPUT_DRIFT")
        try:
            observed_root = Path(observed_path).resolve(strict=True)
            requested_root = (request.repository_root / ".beads").resolve(strict=True)
            database_path = Path(database).resolve(strict=True)
        except OSError:
            raise SafeBdError("BD_OUTPUT_DRIFT") from None
        if observed_root != requested_root:
            raise SafeBdError("BD_OUTPUT_DRIFT")
        try:
            database_path.relative_to(observed_root)
        except ValueError:
            raise SafeBdError("BD_OUTPUT_DRIFT") from None
        if (
            "schema_version" in records[0]
            and type(records[0]["schema_version"]) is not int
        ):
            raise SafeBdError("BD_OUTPUT_DRIFT")
        if "prefix" in records[0] and not isinstance(records[0]["prefix"], str):
            raise SafeBdError("BD_OUTPUT_DRIFT")
    elif request.profile == "workspace_status":
        _require_fields(records[0], frozenset({"schema_version", "summary"}))
        summary = records[0]["summary"]
        if (
            type(records[0]["schema_version"]) is not int
            or not isinstance(summary, dict)
            or set(summary) - _STATUS_SUMMARY_FIELDS
            or not all(
                _exact_integer(item, nullable=False) for item in summary.values()
            )
        ):
            raise SafeBdError("BD_OUTPUT_DRIFT")
    elif request.profile == "dependency_cycles":
        _require_fields(records[0], frozenset({"cycles", "count"}))
        cycles = records[0]["cycles"]
        if (
            not isinstance(cycles, list)
            or type(records[0]["count"]) is not int
            or records[0]["count"] != len(cycles)
        ):
            raise SafeBdError("BD_OUTPUT_DRIFT")
    elif request.profile in {"children_list", "issue_list", "ready_list"}:
        for record in records:
            _require_fields(record, frozenset({"id", "title", "status"}))
            _validate_issue_record(record)
    elif request.profile == "blocked_list":
        for record in records:
            _require_fields(record, frozenset({"id", "blocked_by", "blocked_by_count"}))
            _validate_issue_record(record)
    elif request.profile == "dependency_list":
        for record in records:
            _require_fields(record, frozenset({"id", "dependency_type"}))
            _validate_issue_record(record)
    elif request.profile == "issue_comments":
        for record in records:
            _require_fields(record, frozenset({"issue_id", "text"}))
            if not isinstance(record["issue_id"], str) or not isinstance(
                record["text"], str
            ):
                raise SafeBdError("BD_OUTPUT_DRIFT")
    elif request.profile == "issue_history":
        for record in records:
            _require_fields(
                record, frozenset({"CommitDate", "CommitHash", "Committer", "Issue"})
            )
            if not all(
                isinstance(record[key], str)
                for key in ("CommitDate", "CommitHash", "Committer")
            ) or not isinstance(record["Issue"], dict):
                raise SafeBdError("BD_OUTPUT_DRIFT")
            _validate_issue_record(record["Issue"])
    elif request.profile.startswith("gate_"):
        for record in records:
            for key, item in record.items():
                if key == "timeout":
                    valid = _exact_integer(item)
                else:
                    valid = _string(item)
                if not valid:
                    raise SafeBdError("BD_OUTPUT_DRIFT")
    elif request.profile != "human_list":
        for record in records:
            _validate_issue_record(record)
    else:
        for record in records:
            _validate_issue_record(record)
    return _sanitize_tree(value, sensitive=sensitive)


def _probe_text(
    executable: Path, repository_root: Path, argv: tuple[str, ...]
) -> str | None:
    spec = safe_output.CommandSpec(
        "beads",
        (str(executable), *argv),
        repository_root,
        None,
        30,
        MAX_NATIVE_BYTES,
        MAX_NATIVE_BYTES,
        MAX_NATIVE_BYTES,
        "utf8_text",
        None,
        None,
    )
    result, text = safe_output.run_command(
        spec,
        sensitive=safe_output.SensitiveSet(()),
        callback=lambda out, err: out + err,
    )
    return text if result.status == "SUCCESS" else None


def _verify_executable_contract(
    executable: Path, repository_root: Path, profile: str
) -> str | None:
    version_text = _probe_text(executable, repository_root, ("version",))
    if version_text is None:
        return "UNSUPPORTED_BD_VERSION"
    match = re.fullmatch(
        r"bd version ([0-9]+\.[0-9]+\.[0-9]+)(?: \([^\r\n()]+\))?\n?",
        version_text,
    )
    if match is None or match.group(1) != PINNED_BD_VERSION:
        return "UNSUPPORTED_BD_VERSION"
    contract = PROFILE_CONTRACT_COMMANDS.get(profile)
    if contract is None:
        return None
    contract_text = _probe_text(
        executable, repository_root, tuple(contract.split()[1:])
    )
    if contract_text is None:
        return "CLI_CONTRACT_DRIFT"
    digest = hashlib.sha256(contract_text.encode("utf-8")).hexdigest()
    if digest != CLI_CONTRACT_HASHES[contract]:
        return "CLI_CONTRACT_DRIFT"
    return None


def _native_error(profile: str, code: str) -> SafeBdResult:
    return SafeBdResult(
        "beads.safe-bd-result.v1",
        profile,
        "native_error",
        PINNED_BD_VERSION,
        None,
        None,
        (),
        code,
    )


def run_profile(
    request: SafeBdRequest,
    *,
    sensitive: safe_output.SensitiveSet = safe_output.SensitiveSet(()),
) -> SafeBdResult:
    profile = PROFILES[request.profile]
    executable = shutil.which("bd")
    if executable is None:
        return _native_error(request.profile, "BD_UNAVAILABLE")
    executable_path = Path(executable).resolve()
    contract_error = _verify_executable_contract(
        executable_path, request.repository_root, request.profile
    )
    if contract_error is not None:
        return _native_error(request.profile, contract_error)
    argv = build_argv(request, executable=executable_path)
    stdin = None
    if request.profile == "append_marker_note":
        stdin = str(request.arguments["content"]).encode("utf-8")
    spec = safe_output.CommandSpec(
        "beads",
        argv,
        request.repository_root,
        stdin,
        profile.timeout,
        MAX_NATIVE_BYTES,
        MAX_NATIVE_BYTES,
        MAX_NATIVE_BYTES,
        "utf8_text",
        None,
        None,
    )
    result, data = safe_output.run_command(
        spec,
        sensitive=sensitive,
        callback=lambda out, err: decode_output(
            request,
            out if profile.codec == "json" else out + err,
            sensitive=sensitive,
        ),
    )
    if result.status == "SUCCESS":
        status = "ok"
        error = None
    elif result.status == "OUTPUT_LIMIT":
        status = "output_limit"
        error = "BD_OUTPUT_LIMIT"
    elif result.status == "REDACTION_FAILED":
        status = "redaction_failed"
        error = "BD_REDACTION_FAILED"
    else:
        status = "native_error"
        error = "BD_NATIVE_ERROR"
    return SafeBdResult(
        "beads.safe-bd-result.v1",
        request.profile,
        status,
        PINNED_BD_VERSION,
        None,
        data if status == "ok" else None,
        (),
        error,
    )


# This API is an accidental-misuse guard, not a same-UID sandbox. Arbitrary child
# processes can still invoke bd directly; promotion must keep that limitation open.
DIRECT_WORKER_BD_PREVENTION = "release_blocking_without_os_sandbox_or_pre_tool_hook"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("read", "validate-profile"):
        command = sub.add_parser(name)
        command.add_argument("--request", type=Path, required=True)
        command.add_argument("--worker-packet", type=Path)
    args = parser.parse_args(argv)
    try:
        value = schema_runtime.strict_json_loads(
            schema_runtime.read_bounded(args.request, 65536), max_bytes=65536
        )
        if not isinstance(value, dict) or set(value) != {
            "profile",
            "arguments",
            "repository_root",
        }:
            raise SafeBdError("INVALID_REQUEST")
        worker = None
        if args.worker_packet is not None:
            import validate_worker_packet

            packet = validate_worker_packet.validate_packet(
                schema_runtime.read_bounded(args.worker_packet, 65536)
            )
            worker = WorkerScope(packet.allowed_issue_ids)
        request = SafeBdRequest(
            value["profile"], value["arguments"], Path(value["repository_root"]), worker
        )
        if PROFILES[request.profile].mutation:
            raise SafeBdError("CLI_MUTATION_REFUSED")
        if args.command == "validate-profile":
            print(
                json.dumps(
                    {"status": "valid", "profile": request.profile},
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            return 0
        result = run_profile(request)
        print(json.dumps(asdict(result), sort_keys=True, separators=(",", ":")))
        return 0 if result.status == "ok" else 1
    except (OSError, ValueError, TypeError, schema_runtime.JsonLoadFailure):
        print('{"status":"refused","error_code":"SAFE_BD_REFUSED"}')
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
