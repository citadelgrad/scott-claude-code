"""Tests for the real Hermes skill-discovery harness."""

from __future__ import annotations

import importlib.util
import json
import os
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
        "raw_stdout_path",
        "raw_stderr_path",
        "passed",
        "errored",
        "outcome",
    }
    assert report["raw_content_retained"] is False


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


def _result(harness, scenario_id: str, **overrides):
    fields = {
        "scenario_id": scenario_id,
        "split": "public",
        "expected_load": True,
        "discovery_events": 1,
        "load_events": 1,
        "exit_code": 0,
        "stdout_sha256": "a" * 64,
        "stderr_sha256": "b" * 64,
        "state_sha256": "c" * 64,
    }
    fields.update(overrides)
    return harness.ScenarioResult(**fields)


def test_outcome_is_a_strict_three_way_partition(harness) -> None:
    # A negative scenario that correctly declined to load is a PASS, not a
    # failure. The old predicate required load_events > 0 for every lane, so
    # every correct negative was scored as a routing failure.
    correct_negative = _result(
        harness, "B001", expected_load=False, discovery_events=1, load_events=0
    )
    assert correct_negative.outcome == "passed"
    assert correct_negative.passed is True
    assert correct_negative.errored is False

    # The same lane, but the session never completed. That is an error, not
    # evidence about routing.
    crashed = _result(
        harness,
        "B001",
        expected_load=False,
        discovery_events=0,
        load_events=0,
        exit_code=1,
    )
    assert crashed.outcome == "errored"
    assert crashed.passed is False

    # A real routing failure: the session completed, but loaded when it should
    # not have.
    misrouted = _result(harness, "B002", expected_load=False, load_events=1)
    assert misrouted.outcome == "failed"

    # A positive scenario that failed to load is also a real routing failure.
    missed = _result(harness, "B003", expected_load=True, load_events=0)
    assert missed.outcome == "failed"


def test_full_provider_outage_reports_errored_not_routing_failures(
    tmp_path: Path, harness
) -> None:
    corpus = _full_fixture(tmp_path, harness)
    results = [
        _result(
            harness,
            f"B{index:03d}",
            discovery_events=0,
            load_events=0,
            exit_code=1,
        )
        for index in range(1, harness.SCENARIO_COUNT + 1)
    ]
    report = harness.build_report(
        results,
        candidate_sha256="d" * 64,
        corpus_sha256=harness.sha256_file(corpus),
        hermes_commit=harness.FROZEN_HERMES_COMMIT,
        provider="openai-codex",
        model="gpt-5.6-sol",
        default_profile_unchanged=True,
    )
    # The exact confusion this partition exists to remove: a total outage must
    # never read as "72 lanes proved the skill does not route".
    assert report["errored"] == harness.SCENARIO_COUNT
    assert report["failed"] == 0
    assert report["passed"] == 0
    assert report["scored_count"] == 0
    assert report["schema_version"].endswith(".v2")


def _stub_run_corpus(harness, monkeypatch, tmp_path: Path, outcomes):
    """Wire run_corpus onto a scripted list of per-lane (exit_code, load)."""
    monkeypatch.setattr(harness, "verify_frozen_hermes", lambda _: None)
    monkeypatch.setattr(harness, "verify_default_write_denial", lambda *a, **k: None)
    calls: list[str] = []

    def fake_run_scenario(scenario, **kwargs):
        calls.append(scenario.scenario_id)
        exit_code, load_events = outcomes[len(calls) - 1]
        retention_root = kwargs.get("retention_root")
        retain = retention_root is not None and exit_code != 0
        return _result(
            harness,
            scenario.scenario_id,
            split=scenario.split,
            expected_load=scenario.expected_load,
            discovery_events=1 if exit_code == 0 else 0,
            load_events=load_events,
            exit_code=exit_code,
            raw_stdout_path=(
                harness._retain_stream(
                    retention_root, scenario.scenario_id, "stdout", "x" * 128
                )
                if retain
                else None
            ),
            raw_stderr_path=(
                harness._retain_stream(
                    retention_root, scenario.scenario_id, "stderr", ""
                )
                if retain
                else None
            ),
        )

    monkeypatch.setattr(harness, "_run_scenario", fake_run_scenario)
    corpus = _full_fixture(tmp_path, harness)
    scenarios = harness.load_corpus(corpus, DESIGN, harness.sha256_file(corpus))
    default_home = tmp_path / "default-home"
    default_home.mkdir()
    return calls, scenarios, corpus, default_home


def test_preflight_aborts_on_a_systemic_first_lane_error(
    tmp_path: Path, harness, monkeypatch
) -> None:
    calls, scenarios, corpus, default_home = _stub_run_corpus(
        harness, monkeypatch, tmp_path, [(1, 0)] * harness.SCENARIO_COUNT
    )
    report = harness.run_corpus(
        scenarios=scenarios,
        candidate=SKILL,
        candidate_sha256="d" * 64,
        corpus_sha256=harness.sha256_file(corpus),
        hermes_source=tmp_path,
        runtime_root=tmp_path / "runtime",
        default_home=default_home,
        credentials=None,
        provider="openai-codex",
        model="gpt-5.6-sol",
    )
    # One canary lane, then stop. The old loop burned all 72 lanes of provider
    # budget repeating one systemic failure.
    assert calls == ["B001"]
    assert report["aborted"] is True
    assert report["aborted_reason"].startswith("PREFLIGHT_LANE_ERRORED")
    assert report["errored"] == 1
    assert report["scenario_count"] == 1


def test_force_full_sweep_overrides_the_preflight_abort(
    tmp_path: Path, harness, monkeypatch
) -> None:
    calls, scenarios, corpus, default_home = _stub_run_corpus(
        harness, monkeypatch, tmp_path, [(1, 0)] * harness.SCENARIO_COUNT
    )
    report = harness.run_corpus(
        scenarios=scenarios,
        candidate=SKILL,
        candidate_sha256="d" * 64,
        corpus_sha256=harness.sha256_file(corpus),
        hermes_source=tmp_path,
        runtime_root=tmp_path / "runtime",
        default_home=default_home,
        credentials=None,
        provider="openai-codex",
        model="gpt-5.6-sol",
        force_full_sweep=True,
    )
    assert len(calls) == harness.SCENARIO_COUNT
    assert "aborted" not in report
    assert report["errored"] == harness.SCENARIO_COUNT


def test_a_healthy_first_lane_does_not_abort_the_sweep(
    tmp_path: Path, harness, monkeypatch
) -> None:
    # A routing failure is a measurement, not a systemic fault: it must never
    # trip the canary.
    calls, scenarios, corpus, default_home = _stub_run_corpus(
        harness, monkeypatch, tmp_path, [(0, 0)] * harness.SCENARIO_COUNT
    )
    report = harness.run_corpus(
        scenarios=scenarios,
        candidate=SKILL,
        candidate_sha256="d" * 64,
        corpus_sha256=harness.sha256_file(corpus),
        hermes_source=tmp_path,
        runtime_root=tmp_path / "runtime",
        default_home=default_home,
        credentials=None,
        provider="openai-codex",
        model="gpt-5.6-sol",
    )
    assert len(calls) == harness.SCENARIO_COUNT
    assert "aborted" not in report
    assert report["errored"] == 0
    assert report["failed"] + report["passed"] == harness.SCENARIO_COUNT


def test_raw_retention_is_bounded_private_and_survives_lane_teardown(
    tmp_path: Path, harness, monkeypatch
) -> None:
    calls, scenarios, corpus, default_home = _stub_run_corpus(
        harness, monkeypatch, tmp_path, [(1, 0)] * harness.SCENARIO_COUNT
    )
    runtime_root = tmp_path / "runtime"
    report = harness.run_corpus(
        scenarios=scenarios,
        candidate=SKILL,
        candidate_sha256="d" * 64,
        corpus_sha256=harness.sha256_file(corpus),
        hermes_source=tmp_path,
        runtime_root=runtime_root,
        default_home=default_home,
        credentials=None,
        provider="openai-codex",
        model="gpt-5.6-sol",
        force_full_sweep=True,
    )
    assert report["raw_content_retained"] is True
    retained = [row for row in report["results"] if row["raw_stdout_path"] is not None]
    # Only a bounded number of errored lanes keep raw output, so a full sweep
    # of a broken provider cannot fill the disk.
    assert len(retained) == harness.RAW_RETENTION_LANE_LIMIT
    # Errored lanes past the cap write nothing at all: one stdout and one
    # stderr file per retained lane, and no more.
    store = runtime_root.resolve() / "retained"
    assert len(list(store.iterdir())) == 2 * harness.RAW_RETENTION_LANE_LIMIT
    for row in retained:
        stored = Path(row["raw_stdout_path"])
        # The retention root is a sibling of the lanes, so it outlives the
        # rmtree that tears each lane down right after its run.
        assert stored.parent == runtime_root.resolve() / "retained"
        assert stored.is_file()
        assert stored.stat().st_mode & 0o777 == 0o600
    # Raw text is a path in the report, never a value in it.
    assert "xxxx" not in json.dumps(report)


def test_run_scenario_retains_an_errored_lane_on_a_frozen_result(
    tmp_path: Path, harness, monkeypatch
) -> None:
    # ScenarioResult is frozen, so retention paths must be supplied to the
    # constructor. Assigning them afterwards raises FrozenInstanceError on the
    # first errored lane -- exactly the lane this feature exists to serve.
    scenario = harness.Scenario("B001", "public", "positive", "private prompt", True)
    fake_executable = tmp_path / "hermes"
    fake_executable.write_text("fixture\n", encoding="utf-8")
    fake_executable.chmod(0o700)

    def fake_run(command, **kwargs):
        return SimpleNamespace(returncode=1, stdout="provider quota text", stderr="429")

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
        retention_root=tmp_path / "retained",
    )
    assert result.outcome == "errored"
    stored = Path(result.raw_stdout_path)
    assert stored.read_text(encoding="utf-8") == "provider quota text"
    assert stored.stat().st_mode & 0o777 == 0o600
    assert Path(result.raw_stderr_path).read_text(encoding="utf-8") == "429"


def test_retained_stream_truncates_at_the_byte_cap(tmp_path: Path, harness) -> None:
    root = tmp_path / "retained"
    oversized = "y" * (harness.RAW_RETENTION_MAX_BYTES + 4096)
    path = Path(harness._retain_stream(root, "B001", "stdout", oversized))
    raw = path.read_bytes()
    # The cap is a hard ceiling on what lands on disk, and the marker that
    # records the clip is paid for out of the same budget.
    assert len(raw) == harness.RAW_RETENTION_MAX_BYTES
    assert raw.startswith(b"yyyy")
    assert raw.endswith(b" bytes of raw output clipped]\n")
    assert root.stat().st_mode & 0o777 == 0o700
    assert path.stat().st_mode & 0o777 == 0o600

    # A stream inside the cap is retained whole, with no marker.
    small = Path(harness._retain_stream(root, "B002", "stderr", "short"))
    assert small.read_bytes() == b"short"


def test_report_writer_pins_mode_0600_under_a_hostile_umask(
    tmp_path: Path, harness
) -> None:
    target = tmp_path / "report.json"
    previous = os.umask(0o000)
    try:
        harness._write_json(target, {"schema_version": "x"})
    finally:
        os.umask(previous)
    # os.open's mode argument is masked by the umask, so the explicit chmod
    # after the rename is what actually pins this.
    assert target.stat().st_mode & 0o777 == 0o600
    assert json.loads(target.read_text(encoding="utf-8")) == {"schema_version": "x"}
    assert not list(tmp_path.glob("*.tmp"))


def test_report_writer_without_a_path_still_streams_to_stdout(
    tmp_path: Path, harness, capsys
) -> None:
    harness._write_json(None, {"schema_version": "x"})
    assert json.loads(capsys.readouterr().out) == {"schema_version": "x"}
    assert list(tmp_path.iterdir()) == []


def test_exit_codes_never_report_an_outage_as_success(
    tmp_path: Path, harness, monkeypatch
) -> None:
    monkeypatch.setattr(harness, "require_authorization", lambda *a, **k: None)
    monkeypatch.setattr(harness, "load_corpus", lambda *a, **k: [])
    payloads: dict[str, object] = {}

    def fake_run_corpus(**kwargs):
        return payloads

    monkeypatch.setattr(harness, "run_corpus", fake_run_corpus)

    def invoke(payload) -> int:
        payloads.clear()
        payloads.update(payload)
        return harness.main(
            [
                "run",
                "--candidate",
                str(SKILL),
                "--candidate-sha256",
                "d" * 64,
                "--corpus",
                str(tmp_path / "corpus.json"),
                "--corpus-sha256",
                "e" * 64,
                "--design",
                str(DESIGN),
                "--hermes-source",
                str(tmp_path),
                "--runtime-root",
                str(tmp_path / "runtime"),
                "--default-home",
                str(tmp_path / "default"),
                "--authorization",
                "ignored",
                "--output",
                str(tmp_path / "out.json"),
            ]
        )

    assert invoke({"errored": 0, "failed": 0, "passed": 72}) == 0
    # A measured routing failure.
    assert invoke({"errored": 0, "failed": 4, "passed": 68}) == 3
    # A total outage leaves failed == 0. Exiting 0 here would report the
    # outage as a clean pass.
    assert invoke({"errored": 72, "failed": 0, "passed": 0}) == 4
    assert invoke({"errored": 1, "failed": 0, "passed": 0, "aborted": True}) == 4
