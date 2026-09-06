"""Workspace-health fixtures for the solo lifecycle (scc-0pu.15 / t14).

Exercises ``safe_bd.decode_output``/``workspace_identity_sha256`` against real
on-disk ``.beads`` directories, and ``safe_bd.run_profile`` against a real
(fake) ``bd`` executable, mirroring the proven idioms in the frozen sibling
suite ``beads_contract/test_safe_bd.py`` (t04 / scc-0pu.5) with distinct
fixture framing. Covers:

- AC-T14-001: a healthy workspace round trip produces exact readback with
  zero tracked mutation.
- AC-T14-002: missing/relocated workspaces and native version/contract
  failures produce exact blocked outcomes, never a false success.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat

import pytest

from . import _common as common

m = common.modules()
safe_bd = m.safe_bd


def _snapshot(directory):
    """Hash the full tree under ``directory`` to prove zero mutation."""
    entries = []
    for root, _dirs, files in os.walk(directory):
        for name in sorted(files):
            path = os.path.join(root, name)
            entries.append((os.path.relpath(path, directory), os.path.getsize(path)))
    return tuple(sorted(entries))


def _beads_repo(tmp_path):
    repo = tmp_path / "repo"
    beads = repo / ".beads"
    beads.mkdir(parents=True)
    (beads / "issues.db").write_text("not a real sqlite file, just a marker\n")
    return repo, beads


def _where_request(repo):
    return safe_bd.SafeBdRequest("workspace_where", {}, repo, None)


def _status_request(repo):
    return safe_bd.SafeBdRequest("workspace_status", {}, repo, None)


def test_healthy_workspace_where_round_trip_is_zero_mutation(tmp_path):
    repo, beads = _beads_repo(tmp_path)
    before = _snapshot(repo)
    raw = json.dumps(
        {
            "schema_version": 1,
            "path": str(beads),
            "database_path": str(beads / "issues.db"),
            "prefix": "scc",
        }
    )
    observation = safe_bd.decode_output(_where_request(repo), raw)
    assert observation["prefix"] == "scc"
    assert observation["path"] == str(beads.resolve())
    expected_sha = hashlib.sha256(
        json.dumps(
            {
                "database_path": str((beads / "issues.db").resolve()),
                "prefix": "scc",
                "repository_root": str(repo.resolve()),
                "schema_version": 1,
                "workspace_path": str(beads.resolve()),
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    assert (
        safe_bd.workspace_identity_sha256(_where_request(repo), observation)
        == expected_sha
    )
    assert _snapshot(repo) == before


def test_healthy_workspace_status_round_trip_is_zero_mutation(tmp_path):
    repo, _beads = _beads_repo(tmp_path)
    before = _snapshot(repo)
    raw = json.dumps(
        {
            "schema_version": 1,
            "summary": {
                "total_issues": 12,
                "open_issues": 5,
                "in_progress_issues": 1,
                "closed_issues": 6,
                "ready_issues": 3,
                "blocked_issues": 0,
                "deferred_issues": 0,
                "pinned_issues": 0,
                "epics_eligible_for_closure": 0,
                "average_lead_time_hours": 4,
            },
        }
    )
    observation = safe_bd.decode_output(_status_request(repo), raw)
    assert observation["summary"]["total_issues"] == 12
    assert _snapshot(repo) == before


def test_missing_beads_directory_blocks_workspace_identity(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    raw = json.dumps(
        {
            "schema_version": 1,
            "path": str(repo / ".beads"),
            "database_path": str(repo / ".beads" / "issues.db"),
            "prefix": "scc",
        }
    )
    with pytest.raises(safe_bd.SafeBdError) as excinfo:
        safe_bd.decode_output(_where_request(repo), raw)
    assert excinfo.value.args[0] == "BD_OUTPUT_DRIFT"


def test_relocated_workspace_path_is_drift_not_silent_success(tmp_path):
    repo, _beads = _beads_repo(tmp_path)
    rogue = tmp_path / "rogue" / ".beads"
    rogue.mkdir(parents=True)
    (rogue / "issues.db").write_text("attacker-controlled workspace\n")
    raw = json.dumps(
        {
            "schema_version": 1,
            "path": str(rogue),
            "database_path": str(rogue / "issues.db"),
            "prefix": "scc",
        }
    )
    with pytest.raises(safe_bd.SafeBdError) as excinfo:
        safe_bd.decode_output(_where_request(repo), raw)
    assert excinfo.value.args[0] == "BD_OUTPUT_DRIFT"


def test_malformed_status_summary_field_is_drift(tmp_path):
    repo, _beads = _beads_repo(tmp_path)
    raw = json.dumps(
        {
            "schema_version": 1,
            "summary": {"total_issues": 12, "not_a_real_field": 1},
        }
    )
    with pytest.raises(safe_bd.SafeBdError) as excinfo:
        safe_bd.decode_output(_status_request(repo), raw)
    assert excinfo.value.args[0] == "BD_OUTPUT_DRIFT"


def test_malformed_status_summary_negative_value_is_drift(tmp_path):
    repo, _beads = _beads_repo(tmp_path)
    raw = json.dumps(
        {
            "schema_version": 1,
            "summary": {"total_issues": -1},
        }
    )
    with pytest.raises(safe_bd.SafeBdError) as excinfo:
        safe_bd.decode_output(_status_request(repo), raw)
    assert excinfo.value.args[0] == "BD_OUTPUT_DRIFT"


def _write_fake_bd(path, script):
    path.write_text(script)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def test_run_profile_rejects_unverified_bd_binary(tmp_path, monkeypatch):
    repo, _beads = _beads_repo(tmp_path)
    fake = _write_fake_bd(
        tmp_path / "fake-bd",
        "#!/bin/sh\necho 'not bd'\n",
    )
    monkeypatch.setattr(safe_bd.shutil, "which", lambda _name: str(fake))
    request = safe_bd.SafeBdRequest("workspace_where", {}, repo, None)
    result = safe_bd.run_profile(request)
    assert result.status == "native_error"
    assert result.error_code == "UNSUPPORTED_BD_VERSION"
    assert result.data is None


def test_run_profile_rejects_command_contract_drift(tmp_path, monkeypatch):
    repo, _beads = _beads_repo(tmp_path)
    fake = _write_fake_bd(
        tmp_path / "fake-bd",
        "#!/bin/sh\n"
        'if [ "$1" = version ]; then\n'
        "  echo 'bd version 1.2.2 (Fake)'\n"
        'elif [ "$1" = where ] && [ "$2" = --help ]; then\n'
        "  echo 'this help text has drifted from the pinned contract'\n"
        "else\n"
        "  echo 'unexpected invocation' 1>&2\n"
        "  exit 1\n"
        "fi\n",
    )
    monkeypatch.setattr(safe_bd.shutil, "which", lambda _name: str(fake))
    request = safe_bd.SafeBdRequest("workspace_where", {}, repo, None)
    result = safe_bd.run_profile(request)
    assert result.status == "native_error"
    assert result.error_code == "CLI_CONTRACT_DRIFT"
    assert result.data is None


def test_run_profile_reports_unavailable_when_bd_is_not_on_path(tmp_path, monkeypatch):
    repo, _beads = _beads_repo(tmp_path)
    monkeypatch.setattr(safe_bd.shutil, "which", lambda _name: None)
    request = safe_bd.SafeBdRequest("workspace_where", {}, repo, None)
    result = safe_bd.run_profile(request)
    assert result.status == "native_error"
    assert result.error_code == "BD_UNAVAILABLE"
    assert result.data is None
    assert result.workspace_sha256 is None
