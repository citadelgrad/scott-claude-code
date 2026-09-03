from __future__ import annotations

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
    assert len(safe_bd.CLI_CONTRACT_HASHES) == 10


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

    result = safe_bd.run_profile(safe_bd.SafeBdRequest("doctor", {}, tmp_path, None))

    assert result.status == "ok", result
    assert result.data == "embedded diagnostic\n"
