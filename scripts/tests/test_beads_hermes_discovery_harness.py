"""Tests for the real Hermes skill-discovery harness."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "skills/beads/scripts/hermes_discovery_harness.py"
DESIGN = (
    ROOT / "docs/plans/2026-09-02-hermes-beads-skill/benchmark-corpus-design-v1.json"
)
SKILL = ROOT / "skills/beads"


def load_harness():
    spec = importlib.util.spec_from_file_location("beads_hermes_discovery", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def harness():
    return load_harness()


def _full_fixture(tmp_path: Path, harness) -> Path:
    design = json.loads(DESIGN.read_text(encoding="utf-8"))
    scenarios = [
        {
            "scenario_id": row[0],
            "split": row[1],
            "polarity": row[2],
            "prompt": f"private fixture prompt for {row[0]}",
        }
        for row in design["scenarios"]
    ]
    payload = {
        "schema_version": harness.CORPUS_ENVELOPE_SCHEMA,
        "design_sha256": harness.FROZEN_DESIGN_SHA256,
        "split_hashes": harness.FROZEN_SPLIT_HASHES,
        "scenarios": scenarios,
    }
    path = tmp_path / "corpus.json"
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return path


def test_candidate_tree_hash_is_content_bound_and_install_is_exact(
    tmp_path: Path, harness
) -> None:
    expected = harness.hash_tree(SKILL)
    home = tmp_path / "isolated-home"
    installed = harness.install_candidate(SKILL, home, expected)
    assert installed == home / "skills" / "beads"
    assert harness.hash_tree(installed) == expected

    (installed / "SKILL.md").write_text("changed\n", encoding="utf-8")
    with pytest.raises(harness.HarnessError, match="CANDIDATE_HASH_MISMATCH"):
        harness.install_candidate(SKILL, tmp_path / "second-home", "0" * 64)


def test_corpus_loader_requires_all_frozen_scenarios_without_disclosure(
    tmp_path: Path, harness
) -> None:
    corpus = _full_fixture(tmp_path, harness)
    loaded = harness.load_corpus(corpus, DESIGN, harness.sha256_file(corpus))
    assert len(loaded) == 72
    assert loaded[0].scenario_id == "B001"

    payload = json.loads(corpus.read_text(encoding="utf-8"))
    payload["scenarios"].pop()
    corpus.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(harness.HarnessError, match="CORPUS_INDEX_MISMATCH") as error:
        harness.load_corpus(corpus, DESIGN, harness.sha256_file(corpus))
    assert "private fixture prompt" not in str(error.value)


def test_sanitized_report_contains_no_prompt_or_output_content(
    tmp_path: Path, harness
) -> None:
    corpus = _full_fixture(tmp_path, harness)
    scenarios = harness.load_corpus(corpus, DESIGN, harness.sha256_file(corpus))
    result = harness.ScenarioResult(
        scenario_id=scenarios[0].scenario_id,
        split=scenarios[0].split,
        expected_load=True,
        discovery_events=1,
        load_events=1,
        exit_code=0,
        stdout_sha256="a" * 64,
        stderr_sha256="b" * 64,
        state_sha256="c" * 64,
    )
    report = harness.build_report(
        [result],
        candidate_sha256="d" * 64,
        corpus_sha256=harness.sha256_file(corpus),
        hermes_commit=harness.FROZEN_HERMES_COMMIT,
        provider="openai-codex",
        model="gpt-5.6-sol",
        default_profile_unchanged=True,
    )
    rendered = json.dumps(report, sort_keys=True)
    assert "private fixture prompt" not in rendered
    assert set(report["results"][0]) == {
        "scenario_id",
        "split",
        "expected_load",
        "discovery_events",
        "load_events",
        "exit_code",
        "stdout_sha256",
        "stderr_sha256",
        "state_sha256",
        "passed",
    }


def test_authorization_is_exact_and_caps_model_usage(harness) -> None:
    request = harness.approval_request("openai-codex", "gpt-5.6-sol")
    assert request["hermes_sessions"] == 72
    assert request["maximum_provider_requests"] == 144
    assert request["maximum_turns_per_session"] == 2
    assert request["paid_api_fallback"] is False
    with pytest.raises(harness.HarnessError, match="MODEL_RUNS_NOT_AUTHORIZED"):
        harness.require_authorization("yes", "openai-codex", "gpt-5.6-sol")
    harness.require_authorization(
        request["required_authorization_text"], "openai-codex", "gpt-5.6-sol"
    )


def test_scenario_prompt_uses_stdin_and_actual_event_shapes_are_parsed(
    tmp_path: Path, harness, monkeypatch
) -> None:
    prompt = "private hidden sentinel that must never enter argv"
    scenario = harness.Scenario("B001", "public", "positive", prompt, True)
    fake_executable = tmp_path / "hermes"
    fake_executable.write_text("fixture\n", encoding="utf-8")
    fake_executable.chmod(0o700)
    observed = {}

    def fake_run(command, **kwargs):
        observed["command"] = command
        observed["input"] = kwargs["input"]
        home = Path(kwargs["env"]["HERMES_HOME"])
        with sqlite3.connect(home / "state.db") as connection:
            connection.execute(
                "CREATE TABLE messages (role TEXT, tool_calls TEXT, tool_name TEXT)"
            )
            connection.execute(
                "INSERT INTO messages VALUES (?, ?, ?)",
                (
                    "assistant",
                    json.dumps(
                        [
                            {
                                "function": {
                                    "name": "skill_view",
                                    "arguments": json.dumps({"name": "beads"}),
                                }
                            }
                        ]
                    ),
                    None,
                ),
            )
        usage = home / "skills" / ".usage.json"
        usage.write_text(json.dumps({"beads": {"use_count": 1}}), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="private output", stderr="")

    monkeypatch.setattr(harness, "_hermes_executable", lambda _: fake_executable)
    monkeypatch.setattr(harness.subprocess, "run", fake_run)
    lane = tmp_path / "lane"
    lane.mkdir()
    result = harness._run_scenario(
        scenario,
        lane=lane,
        candidate=SKILL,
        candidate_sha256=harness.hash_tree(SKILL),
        hermes_source=tmp_path,
        provider="openai-codex",
        model="gpt-5.6-sol",
        credentials=None,
        default_home=tmp_path / "default-home",
    )
    assert observed["input"] == prompt
    assert prompt not in observed["command"]
    query_index = observed["command"].index("--query-file")
    assert observed["command"][query_index : query_index + 2] == ["--query-file", "-"]
    assert result.discovery_events == 1
    assert result.load_events == 1
    assert result.passed is True


def test_macos_sandbox_denies_default_profile_writes(harness, tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    default = tmp_path / "default"
    default.mkdir()
    command = harness.sandboxed_command(
        ["/usr/bin/true"], runtime_root=runtime, default_home=default
    )
    assert command[0].endswith("sandbox-exec")
    profile = Path(command[2]).read_text(encoding="utf-8")
    assert "(deny file-write*" in profile
    assert str(default.resolve()) in profile
    assert str(runtime.resolve()) in profile
    denied = harness.sandboxed_command(
        ["/usr/bin/touch", str(default / "forbidden")],
        runtime_root=runtime,
        default_home=default,
    )
    completed = subprocess.run(denied, capture_output=True, check=False)
    assert completed.returncode != 0
    assert not (default / "forbidden").exists()


def test_actual_frozen_hermes_probe_loads_exact_candidate_without_default_write(
    tmp_path: Path, harness
) -> None:
    hermes_source = Path.home() / ".hermes" / "hermes-agent"
    if not hermes_source.is_dir():
        pytest.skip("frozen Hermes source is not installed")
    if harness.git_commit(hermes_source) != harness.FROZEN_HERMES_COMMIT:
        pytest.skip("installed Hermes source is not the frozen commit")

    default_home = tmp_path / "default-profile"
    default_home.mkdir()
    sentinel = default_home / "sentinel"
    sentinel.write_text("unchanged\n", encoding="utf-8")
    before = harness.hash_tree(default_home)
    evidence = harness.probe_actual_hermes_install(
        candidate=SKILL,
        candidate_sha256=harness.hash_tree(SKILL),
        hermes_source=hermes_source,
        runtime_root=tmp_path / "runtime",
        default_home=default_home,
    )
    assert evidence["candidate_discovered"] is True
    assert evidence["candidate_loaded"] is True
    assert evidence["installed_candidate_sha256"] == harness.hash_tree(SKILL)
    assert evidence["default_profile_unchanged"] is True
    assert evidence["default_profile_write_denied"] is True
    assert evidence["default_profile_mutation_by_harness"] is False
    assert evidence["automatic_routing_exercised"] is False
    assert evidence["blocker"] == "MODEL_EXECUTION_NOT_AUTHORIZED"
    assert harness.hash_tree(default_home) == before
