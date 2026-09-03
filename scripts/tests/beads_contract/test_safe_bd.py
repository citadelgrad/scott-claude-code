from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
MODULE = ROOT / "skills/beads/scripts/safe_bd.py"


def _load():
    spec = importlib.util.spec_from_file_location("beads_safe_bd", MODULE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_closed_profiles_build_exact_readonly_argv(tmp_path: Path) -> None:
    safe_bd = _load()
    request = safe_bd.SafeBdRequest(
        "issue_get", {"issue_id": "-odd-id"}, tmp_path, None
    )
    argv = safe_bd.build_argv(request, executable=Path("/usr/local/bin/bd"))
    assert argv == ("/usr/local/bin/bd", "--readonly", "--json", "show", "--id=-odd-id")
    with pytest.raises(safe_bd.SafeBdError, match="UNKNOWN_PROFILE"):
        safe_bd.SafeBdRequest("anything", {}, tmp_path, None)
    with pytest.raises(safe_bd.SafeBdError, match="UNKNOWN_ARGUMENT"):
        safe_bd.SafeBdRequest("ready_list", {"shell": True}, tmp_path, None)
    resolved_homebrew = safe_bd.build_argv(
        request, executable=Path("/opt/homebrew/Cellar/beads/1.2.2/bin/beads")
    )
    assert resolved_homebrew[0].endswith("/bin/beads")


def test_snapshot_profiles_build_exact_scoped_ordered_argv(tmp_path: Path) -> None:
    safe_bd = _load()
    executable = Path("/usr/local/bin/bd")

    issue_list = safe_bd.SafeBdRequest(
        "issue_list",
        {
            "issue_ids": ["scc-b", "scc-a"],
            "limit": 0,
            "sort": "id",
            "reverse": True,
            "issue_type": "task",
        },
        tmp_path,
        None,
    )
    assert safe_bd.build_argv(issue_list, executable=executable) == (
        "/usr/local/bin/bd",
        "--readonly",
        "--json",
        "list",
        "--limit=0",
        "--id=scc-b,scc-a",
        "--sort=id",
        "--reverse",
        "--type=task",
    )

    ready = safe_bd.SafeBdRequest(
        "ready_list",
        {"parent": "scc-root", "limit": 0, "sort": "priority", "issue_type": "task"},
        tmp_path,
        None,
    )
    assert safe_bd.build_argv(ready, executable=executable)[-4:] == (
        "--limit=0",
        "--parent=scc-root",
        "--sort=priority",
        "--type=task",
    )

    dependency = safe_bd.SafeBdRequest(
        "dependency_list",
        {"issue_id": "scc-a", "direction": "up", "dependency_type": "blocks"},
        tmp_path,
        None,
    )
    assert safe_bd.build_argv(dependency, executable=executable)[-5:] == (
        "list",
        "--direction=up",
        "--type=blocks",
        "--",
        "scc-a",
    )


@pytest.mark.parametrize(
    ("profile", "arguments"),
    [
        ("issue_list", {"issue_ids": ["scc-a,b"]}),
        ("issue_list", {"sort": "unsupported"}),
        ("ready_list", {"sort": "id"}),
        ("ready_list", {"issue_type": "chore"}),
        ("issue_list", {"status": "unknown"}),
        ("dependency_list", {"issue_id": "scc-a", "direction": "sideways"}),
        ("dependency_list", {"issue_id": "scc-a", "dependency_type": "unknown"}),
    ],
)
def test_snapshot_profiles_fail_closed_on_unrepresentable_options(
    tmp_path: Path, profile: str, arguments: dict[str, object]
) -> None:
    safe_bd = _load()
    with pytest.raises(safe_bd.SafeBdError):
        safe_bd.SafeBdRequest(profile, arguments, tmp_path, None)


def test_worker_scope_refuses_mutation_and_unlisted_issue(tmp_path: Path) -> None:
    safe_bd = _load()
    worker = safe_bd.WorkerScope(frozenset({"scc-main", "scc-pre"}))
    read = safe_bd.SafeBdRequest(
        "issue_get", {"issue_id": "scc-main"}, tmp_path, worker
    )
    assert "--readonly" in safe_bd.build_argv(read, executable=Path("/opt/bd"))
    with pytest.raises(safe_bd.SafeBdError, match="WORKER_TARGET_REFUSED"):
        safe_bd.SafeBdRequest("issue_get", {"issue_id": "scc-other"}, tmp_path, worker)
    with pytest.raises(safe_bd.SafeBdError, match="WORKER_MUTATION_REFUSED"):
        safe_bd.SafeBdRequest(
            "close_exact",
            {"issue_id": "scc-main", "reason": "done", "actor": "parent"},
            tmp_path,
            worker,
        )


def test_pinned_live_contract_hashes_are_complete() -> None:
    safe_bd = _load()
    assert (
        safe_bd.CLI_CONTRACT_HASHES["bd prime"]
        == "993713b7b4f06101731ee8e8efb3f0e692858c273525a91ae4bb8b3b053384ba"
    )
    assert (
        safe_bd.CLI_CONTRACT_HASHES["bd show --help"]
        == "010c34cfe1bf28beedf9c90978979a957e612ce8bd8a56c5b47e2cdde5038179"
    )
    assert len(safe_bd.CLI_CONTRACT_HASHES) == 13


def test_native_json_unknown_fields_and_floats_fail_closed(tmp_path: Path) -> None:
    safe_bd = _load()
    request = safe_bd.SafeBdRequest(
        "issue_get", {"issue_id": "scc-main"}, tmp_path, None
    )
    assert (
        safe_bd.decode_output(
            request,
            '[{"id":"scc-main","title":"ok","status":"open","priority":1,"issue_type":"task","assignee":null,"parent":null,"dependencies":[],"created_by":"tester","started_at":null}]',
        )[0]["id"]
        == "scc-main"
    )
    with pytest.raises(safe_bd.SafeBdError, match="BD_OUTPUT_DRIFT"):
        safe_bd.decode_output(request, '[{"id":"scc-main","unknown":true}]')
    with pytest.raises(safe_bd.SafeBdError, match="BD_OUTPUT_MALFORMED"):
        safe_bd.decode_output(request, '[{"id":"scc-main","priority":1.0}]')
    with pytest.raises(safe_bd.SafeBdError, match="BD_OUTPUT_DRIFT"):
        safe_bd.decode_output(request, "{}")


def test_issue_records_allow_real_defer_and_due_fields_with_exact_types(
    tmp_path: Path,
) -> None:
    safe_bd = _load()
    request = safe_bd.SafeBdRequest(
        "issue_get", {"issue_id": "scc-main"}, tmp_path, None
    )
    output = (
        '[{"id":"scc-main","title":"ok","status":"deferred",'
        '"defer_until":"2026-09-04T12:00:00Z","due_at":null}]'
    )
    result = safe_bd.decode_output(request, output)
    assert result[0]["defer_until"] == "2026-09-04T12:00:00Z"
    assert result[0]["due_at"] is None

    for field, value in (("defer_until", 7), ("due_at", False)):
        malformed = json.dumps(
            {
                "id": "scc-main",
                "title": "ok",
                "status": "deferred",
                field: value,
            }
        )
        with pytest.raises(safe_bd.SafeBdError, match="BD_OUTPUT_DRIFT"):
            safe_bd.decode_output(request, f"[{malformed}]")


def test_nested_credential_fields_are_redacted(tmp_path: Path) -> None:
    safe_bd = _load()
    request = safe_bd.SafeBdRequest(
        "issue_get", {"issue_id": "scc-main"}, tmp_path, None
    )
    sentinel = "synthetic-secret-value-0123456789"

    result = safe_bd.decode_output(
        request,
        '[{"id":"scc-main","title":"x","status":"open","metadata":{"token":"'
        + sentinel
        + '","token=embedded-secret-0123456789":"ignored"}}]',
    )

    encoded = repr(result)
    assert sentinel not in encoded
    assert "embedded-secret-0123456789" not in encoded
    assert "[REDACTED]" in encoded


def test_live_v122_read_shapes_are_explicitly_supported(tmp_path: Path) -> None:
    safe_bd = _load()
    (tmp_path / ".beads").mkdir()
    where = safe_bd.SafeBdRequest("workspace_where", {}, tmp_path, None)
    assert (
        safe_bd.decode_output(
            where,
            json.dumps(
                {
                    "schema_version": 1,
                    "path": str(tmp_path / ".beads"),
                    "database_path": str(tmp_path / ".beads"),
                    "prefix": "scc",
                }
            ),
        )["prefix"]
        == "scc"
    )

    history = safe_bd.SafeBdRequest(
        "issue_history", {"issue_id": "scc-main"}, tmp_path, None
    )
    assert (
        safe_bd.decode_output(
            history,
            '[{"CommitDate":"now","CommitHash":"abc","Committer":"tester","Issue":{"id":"scc-main"}}]',
        )[0]["CommitHash"]
        == "abc"
    )

    blocked = safe_bd.SafeBdRequest("blocked_list", {}, tmp_path, None)
    assert (
        safe_bd.decode_output(
            blocked,
            '[{"id":"scc-main","blocked_by":[],"blocked_by_count":0}]',
        )[0]["blocked_by_count"]
        == 0
    )

    cycles = safe_bd.SafeBdRequest("dependency_cycles", {}, tmp_path, None)
    assert safe_bd.decode_output(cycles, "[]") == {"cycles": [], "count": 0}

    gates = safe_bd.SafeBdRequest("gate_list", {}, tmp_path, None)
    assert safe_bd.decode_output(gates, "null") == []

    doctor = safe_bd.SafeBdRequest("doctor", {}, tmp_path, None)
    assert safe_bd.decode_output(doctor, "health ok\n") == "health ok\n"


def test_native_shapes_reject_wrong_field_types_and_workspace_mismatch(
    tmp_path: Path,
) -> None:
    safe_bd = _load()
    issue = safe_bd.SafeBdRequest("issue_get", {"issue_id": "scc-main"}, tmp_path, None)
    with pytest.raises(safe_bd.SafeBdError, match="BD_OUTPUT_DRIFT"):
        safe_bd.decode_output(issue, '[{"id":"scc-main","title":7,"status":"open"}]')

    where = safe_bd.SafeBdRequest("workspace_where", {}, tmp_path, None)
    with pytest.raises(safe_bd.SafeBdError, match="BD_OUTPUT_DRIFT"):
        safe_bd.decode_output(
            where,
            '{"path":"/tmp/not-the-requested-repository","database_path":"/tmp/db"}',
        )


def test_configured_sensitive_values_are_redacted_recursively(tmp_path: Path) -> None:
    safe_bd = _load()
    request = safe_bd.SafeBdRequest(
        "issue_get", {"issue_id": "scc-main"}, tmp_path, None
    )
    sentinel = "redaction-probe-value-0123456789"

    result = safe_bd.decode_output(
        request,
        '[{"id":"scc-main","title":"' + sentinel + '","status":"open"}]',
        sensitive=safe_bd.safe_output.SensitiveSet((sentinel,)),
    )

    assert result[0]["title"] == "[REDACTED]"


def test_workspace_hash_is_canonical_bound_and_populated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    safe_bd = _load()
    workspace = tmp_path / ".beads"
    database = workspace / "embeddeddolt"
    database.mkdir(parents=True)
    request = safe_bd.SafeBdRequest(
        "issue_get", {"issue_id": "scc-main"}, tmp_path, None
    )
    observation = {
        "schema_version": 1,
        "path": str(workspace),
        "database_path": str(database),
        "prefix": "scc",
    }
    expected = hashlib.sha256(
        json.dumps(
            {
                "database_path": str(database.resolve()),
                "prefix": "scc",
                "repository_root": str(tmp_path.resolve()),
                "schema_version": 1,
                "workspace_path": str(workspace.resolve()),
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    assert safe_bd.workspace_identity_sha256(request, observation) == expected

    fake = tmp_path / "bd"
    fake.write_text("#!/bin/sh\n", encoding="utf-8")
    os.chmod(fake, 0o700)
    monkeypatch.setattr(safe_bd.shutil, "which", lambda _: str(fake))
    monkeypatch.setattr(safe_bd, "_verify_executable_contract", lambda *_: None)
    monkeypatch.setattr(safe_bd, "_read_workspace_identity", lambda *_: expected)

    def successful_run(*_args, **kwargs):
        data = kwargs["callback"](
            '[{"id":"scc-main","title":"ok","status":"open"}]', ""
        )
        return type("Result", (), {"status": "SUCCESS"})(), data

    monkeypatch.setattr(safe_bd.safe_output, "run_command", successful_run)
    result = safe_bd.run_profile(request)
    assert result.status == "ok"
    assert result.workspace_sha256 == expected

    changed = dict(observation, prefix="other")
    assert safe_bd.workspace_identity_sha256(request, changed) != expected
    with pytest.raises(safe_bd.SafeBdError, match="BD_OUTPUT_DRIFT"):
        safe_bd.workspace_identity_sha256(
            request, dict(observation, path=str(tmp_path / "elsewhere"))
        )


def test_workspace_identity_accepts_only_registered_git_worktree_workspace(
    tmp_path: Path,
) -> None:
    safe_bd = _load()
    repository = tmp_path / "repo"
    workspace = repository / ".beads"
    database = workspace / "embeddeddolt"
    gitdir = repository / ".git/worktrees/lane"
    lane = repository / ".work/lane"
    database.mkdir(parents=True)
    gitdir.mkdir(parents=True)
    lane.mkdir(parents=True)
    (lane / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
    (gitdir / "gitdir").write_text(f"{lane / '.git'}\n", encoding="utf-8")
    request = safe_bd.SafeBdRequest("workspace_where", {}, lane, None)
    observation = {
        "schema_version": 1,
        "path": str(workspace),
        "database_path": str(database),
        "prefix": "scc",
    }
    assert len(safe_bd.workspace_identity_sha256(request, observation)) == 64

    (gitdir / "gitdir").write_text(f"{tmp_path / 'other/.git'}\n", encoding="utf-8")
    with pytest.raises(safe_bd.SafeBdError, match="BD_OUTPUT_DRIFT"):
        safe_bd.workspace_identity_sha256(request, observation)


def test_create_exact_type_checks_complete_immutable_arguments(tmp_path: Path) -> None:
    safe_bd = _load()
    valid = {
        "issue_id": "scc-new",
        "actor": "parent",
        "title": "New task",
        "issue_type": "task",
        "priority": 1,
    }
    request = safe_bd.SafeBdRequest("create_exact", valid, tmp_path, None)
    assert safe_bd.build_argv(request, executable=Path("/usr/local/bin/bd"))[-4:] == (
        "--id=scc-new",
        "--title=New task",
        "--type=task",
        "--priority=1",
    )
    for field, invalid in (
        ("title", ""),
        ("title", 7),
        ("issue_type", "gate"),
        ("priority", True),
    ):
        arguments = dict(valid)
        arguments[field] = invalid
        with pytest.raises(safe_bd.SafeBdError):
            safe_bd.SafeBdRequest("create_exact", arguments, tmp_path, None)


def test_parent_restore_claim_fields_supports_exact_assignee_and_clear(
    tmp_path: Path,
) -> None:
    safe_bd = _load()
    executable = Path("/usr/local/bin/bd")
    for assignee, expected_flag in (
        ("parent-a", "--assignee=parent-a"),
        (None, "--assignee="),
    ):
        request = safe_bd.SafeBdRequest(
            "restore_claim_fields",
            {
                "issue_id": "scc-main",
                "actor": "coordinator",
                "status": "open",
                "assignee": assignee,
            },
            tmp_path,
            None,
        )
        argv = safe_bd.build_argv(request, executable=executable)
        assert argv == (
            "/usr/local/bin/bd",
            "--sandbox",
            "--json",
            "--actor=coordinator",
            "update",
            "--status=open",
            expected_flag,
            "--",
            "scc-main",
        )
        assert safe_bd.PROFILES[request.profile].mutation is True
        assert safe_bd.PROFILES[request.profile].worker_allowed is False

    for status in ("closed", "pinned", "hooked", "custom", ""):
        with pytest.raises(safe_bd.SafeBdError, match="INVALID_RESTORE_STATUS"):
            safe_bd.SafeBdRequest(
                "restore_claim_fields",
                {
                    "issue_id": "scc-main",
                    "actor": "coordinator",
                    "status": status,
                    "assignee": None,
                },
                tmp_path,
                None,
            )
    with pytest.raises(safe_bd.SafeBdError, match="INVALID_ASSIGNEE"):
        safe_bd.SafeBdRequest(
            "restore_claim_fields",
            {
                "issue_id": "scc-main",
                "actor": "coordinator",
                "status": "open",
                "assignee": 7,
            },
            tmp_path,
            None,
        )


def test_version_probe_ignores_unrelated_sanitized_stderr(tmp_path: Path) -> None:
    safe_bd = _load()
    fake = tmp_path / "bd"
    fake.write_text(
        "#!/bin/sh\nprintf 'bd version 1.2.2 (Homebrew)\\n'\nprintf 'workspace warning\\n' >&2\n",
        encoding="utf-8",
    )
    os.chmod(fake, 0o700)
    assert (
        safe_bd._probe_text(fake, tmp_path, ("version",), include_stderr=False)
        == "bd version 1.2.2 (Homebrew)\n"
    )


def test_run_profile_rejects_unverified_bd_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    safe_bd = _load()
    fake = tmp_path / "bd"
    fake.write_text("#!/bin/sh\nprintf 'not bd\\n'\n", encoding="utf-8")
    os.chmod(fake, 0o700)
    monkeypatch.setattr(safe_bd.shutil, "which", lambda _: str(fake))
    request = safe_bd.SafeBdRequest(
        "issue_get", {"issue_id": "scc-main"}, tmp_path, None
    )

    result = safe_bd.run_profile(request)

    assert result.status == "native_error"
    assert result.error_code == "UNSUPPORTED_BD_VERSION"
    assert result.data is None


def test_run_profile_rejects_command_contract_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    safe_bd = _load()
    fake = tmp_path / "bd"
    fake.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = version ]; then\n'
        "  printf 'bd version 1.2.2 (Homebrew)\\n'\n"
        "else\n"
        "  printf 'drifted help\\n'\n"
        "fi\n",
        encoding="utf-8",
    )
    os.chmod(fake, 0o700)
    monkeypatch.setattr(safe_bd.shutil, "which", lambda _: str(fake))
    request = safe_bd.SafeBdRequest(
        "issue_get", {"issue_id": "scc-main"}, tmp_path, None
    )

    result = safe_bd.run_profile(request)

    assert result.status == "native_error"
    assert result.error_code == "CLI_CONTRACT_DRIFT"
    assert result.data is None


def test_text_profile_accepts_sanitized_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    safe_bd = _load()
    fake = tmp_path / "bd"
    fake.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = version ]; then\n'
        "  printf 'bd version 1.2.2 (Homebrew)\\n'\n"
        "else\n"
        "  printf 'embedded diagnostic\\n' >&2\n"
        "fi\n",
        encoding="utf-8",
    )
    os.chmod(fake, 0o700)
    monkeypatch.setattr(safe_bd.shutil, "which", lambda _: str(fake))
    monkeypatch.setattr(safe_bd, "_verify_executable_contract", lambda *_: None)
    monkeypatch.setattr(safe_bd, "_read_workspace_identity", lambda *_: "a" * 64)

    result = safe_bd.run_profile(safe_bd.SafeBdRequest("doctor", {}, tmp_path, None))

    assert result.status == "ok", result
    assert result.data == "embedded diagnostic\n"
