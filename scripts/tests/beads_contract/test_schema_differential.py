from __future__ import annotations

import copy
import importlib.util
import json
import random
import re
import shutil
import sys
from pathlib import Path
from typing import cast

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "skills/beads/scripts"
SCHEMAS = ROOT / "skills/beads/schemas"
FIXTURES = ROOT / "scripts/tests/fixtures/beads_contract"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        f"contract_{name}", SCRIPTS / f"{name}.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _oracle():
    schemas = {
        path.name: json.loads(path.read_text())
        for path in SCHEMAS.glob("*.schema.json")
    }
    registry = Registry().with_resources(
        (schema["$id"], Resource.from_contents(schema)) for schema in schemas.values()
    )
    return schemas, registry


def test_all_fixtures_differentially_agree() -> None:
    runtime = _load("schema_runtime")
    schemas, registry = _oracle()
    for expected, directory in (
        (True, FIXTURES / "valid"),
        (False, FIXTURES / "invalid"),
    ):
        paths = sorted(directory.glob("*.json"))
        assert len(paths) == 24
        for path in paths:
            schema_name = path.name.replace(".json", ".schema.json")
            value = runtime.strict_json_loads(path.read_bytes())
            generated_valid = not runtime.validate_instance(schema_name, value)
            oracle_valid = Draft202012Validator(
                schemas[schema_name], registry=registry
            ).is_valid(value)
            assert generated_valid == oracle_valid == expected, path


def test_deterministic_root_mutation_property_loop() -> None:
    runtime = _load("schema_runtime")
    schemas, _ = _oracle()
    rng = random.Random(7007)
    names = sorted(schemas)
    rng.shuffle(names)
    for name in names:
        valid = json.loads(
            (FIXTURES / "valid" / name.replace(".schema", "")).read_text()
        )
        assert not runtime.validate_instance(name, valid)
        if isinstance(valid, dict):
            injected = copy.deepcopy(valid)
            injected[f"unknown_{rng.randrange(1_000_000)}"] = True
            assert runtime.validate_instance(name, injected)
            for required in schemas[name].get("required", []):
                deleted = copy.deepcopy(valid)
                deleted.pop(required)
                assert runtime.validate_instance(name, deleted), (name, required)


def test_strict_loader_fail_closed_property_loop() -> None:
    runtime = _load("schema_runtime")
    attacks = [
        b'{"x":1.0}',
        b'{"x":NaN}',
        b'{"x":1,"x":2}',
        b'{"x":"\\ud800"}',
        b'{"x":1} trailing',
        b"\xff",
        b'{"x":"a\x00b"}',
    ]
    for raw in attacks:
        with pytest.raises(runtime.JsonLoadFailure):
            runtime.strict_json_loads(raw)
    composed = runtime.strict_json_loads('"é"'.encode())
    decomposed = runtime.strict_json_loads('"é"'.encode())
    assert composed != decomposed


def test_direct_create_request_carries_complete_typed_immutable_arguments() -> None:
    runtime = _load("schema_runtime")
    schemas, registry = _oracle()
    schema_name = "direct-operation-request-v1.schema.json"
    value = json.loads(
        (FIXTURES / "valid/direct-operation-request-v1.json").read_text()
    )
    value["effect_type"] = "create"
    value["payload"].update(
        {
            "title": "Implement exact create",
            "issue_type": "task",
            "priority": 1,
            "fields_sha256": "b" * 64,
        }
    )
    validator = Draft202012Validator(schemas[schema_name], registry=registry)
    assert not runtime.validate_instance(schema_name, value)
    assert validator.is_valid(value)

    for field, invalid in (
        ("title", None),
        ("title", ""),
        ("issue_type", "unknown"),
        ("priority", 5),
        ("priority", True),
        ("fields_sha256", None),
    ):
        candidate = copy.deepcopy(value)
        candidate["payload"][field] = invalid
        assert runtime.validate_instance(schema_name, candidate), field
        assert not validator.is_valid(candidate), field

    non_create = copy.deepcopy(value)
    non_create["effect_type"] = "claim"
    assert runtime.validate_instance(schema_name, non_create)
    assert not validator.is_valid(non_create)


def test_downstream_coordinator_operations_have_explicit_result_values() -> None:
    runtime = _load("schema_runtime")
    value = json.loads((FIXTURES / "valid/operation-result-v1.json").read_text())
    for operation in (
        "doctor",
        "start_run",
        "front",
        "prepare_action",
        "resolve_action",
        "cleanup_run",
    ):
        candidate = copy.deepcopy(value)
        candidate["operation"] = operation
        assert not runtime.validate_instance(
            "operation-result-v1.schema.json", candidate
        ), operation


def test_checkpoint_artifact_supports_verified_state() -> None:
    runtime = _load("schema_runtime")
    value = json.loads((FIXTURES / "valid/run-checkpoint-v1.json").read_text())
    value["issues"] = {
        "scc-x": {
            "tracker_status_observed": "in_progress",
            "readiness": {"state": "not_ready", "reason": "claimed"},
            "ownership": {
                "state": "held",
                "epoch": 1,
                "token_sha256": "a" * 64,
                "actor": "parent",
            },
            "attempt": {
                "state": "joined",
                "attempt_id": "attempt-001",
                "delegation_id": "d",
                "subagent_id": "s",
                "child_session_id": "c",
            },
            "worker_result": {
                "state": "accepted",
                "outcome": "completed",
                "record_sha256": "b" * 64,
            },
            "artifact": {"state": "verified", "lane_freeze_sha256": "c" * 64},
            "verification": {"state": "passed", "record_sha256": "d" * 64},
            "review": {"state": "not_required", "record_sha256": None},
            "integration": {
                "state": "not_started",
                "candidate_sha256": None,
                "event_id": None,
            },
            "gate": {"state": "none", "gate_id": None},
            "packet_sha256": "e" * 64,
            "result_sha256": "f" * 64,
            "worktree": "/tmp/x",
            "branch": "lane",
            "base_sha": "1" * 40,
            "head_sha": "2" * 40,
        }
    }
    assert not runtime.validate_instance("run-checkpoint-v1.schema.json", value)


def test_generator_check_staleness_and_unsupported_keyword(tmp_path: Path) -> None:
    generator = _load("generate_schema_runtime")
    assert generator.render(SCHEMAS) == (SCRIPTS / "schema_runtime.py").read_bytes()
    generated = tmp_path / "schema_runtime.py"
    assert (
        generator.main(["--schema-dir", str(SCHEMAS), "--output", str(generated)]) == 0
    )
    assert generated.stat().st_mode & 0o777 == 0o644
    copied = tmp_path / "schemas"
    shutil.copytree(SCHEMAS, copied)
    target = copied / "run-request-v1.schema.json"
    value = json.loads(target.read_text())
    value["minProperties"] = 1
    target.write_text(json.dumps(value))
    with pytest.raises(generator.SourceError, match="unsupported keyword"):
        generator.render(copied)
    runtime = _load("schema_runtime")
    target = copied / "run-request-v1.schema.json"
    value.pop("minProperties")
    target.write_text(json.dumps(value) + "\n")
    with pytest.raises(RuntimeError, match="stale"):
        runtime.assert_generated_current(copied)


def test_release_blocking_limitations_are_not_overclaimed() -> None:
    value = json.loads((FIXTURES / "release-blocking-limitations.json").read_text())
    assert value["plugin_free_child_summary"] == {
        "classification": "UNPREVENTABLE_PRE_INTERCEPTION_LEAK",
        "controlled_persistence_rejects_or_redacts": True,
        "prevented_before_parent_model_context": False,
        "release_blocking": True,
    }
    assert value["direct_worker_bd"]["prevented_by_application_helpers"] is False
    assert value["detached_descendant_processes"] == {
        "classification": "release_blocking_without_os_process_containment",
        "prevented_by_application_helpers": False,
        "release_blocking": True,
    }


def test_applied_effects_require_probe_and_readback_evidence() -> None:
    runtime = _load("schema_runtime")
    schemas, registry = _oracle()

    cases: list[tuple[str, dict, list[tuple[str, ...]]]] = []
    direct = json.loads(
        (FIXTURES / "valid/direct-operation-record-v1.json").read_text()
    )
    cases.append(
        (
            "direct-operation-record-v1.schema.json",
            direct,
            [
                ("probe_evidence_path",),
                ("probe_evidence_sha256",),
                ("readback_evidence_path",),
                ("readback_evidence_sha256",),
            ],
        )
    )
    receipt = json.loads((FIXTURES / "valid/harness-receipt-v1.json").read_text())
    cases.append(
        (
            "harness-receipt-v1.schema.json",
            receipt,
            [
                ("evidence", "path"),
                ("evidence", "sha256"),
                ("observed_identity",),
                ("observed_sha256",),
            ],
        )
    )
    journal = json.loads(
        (FIXTURES / "valid/operation-journal-event-v1.json").read_text()
    )
    journal.update(
        phase="RESOLUTION",
        status="APPLIED",
        observed_post_state_sha256="b" * 64,
        readback_evidence_path="/tmp/readback.json",
        readback_evidence_sha256="c" * 64,
        error=None,
    )
    cases.append(
        (
            "operation-journal-event-v1.schema.json",
            journal,
            [
                ("observed_post_state_sha256",),
                ("readback_evidence_path",),
                ("readback_evidence_sha256",),
            ],
        )
    )

    for schema_name, valid, nullable_paths in cases:
        validator = Draft202012Validator(schemas[schema_name], registry=registry)
        assert not runtime.validate_instance(schema_name, valid)
        assert validator.is_valid(valid)
        for path in nullable_paths:
            candidate = copy.deepcopy(valid)
            target = candidate
            for component in path[:-1]:
                target = target[component]
            target[path[-1]] = None
            assert runtime.validate_instance(schema_name, candidate), (
                schema_name,
                path,
            )
            assert not validator.is_valid(candidate), (schema_name, path)


def test_expiring_approval_requires_expiry_timestamp() -> None:
    runtime = _load("schema_runtime")
    schemas, registry = _oracle()
    schema_name = "approval-record-v1.schema.json"
    value = json.loads((FIXTURES / "valid/approval-record-v1.json").read_text())
    value["expiry_policy"] = "expires_at"
    validator = Draft202012Validator(schemas[schema_name], registry=registry)

    assert runtime.validate_instance(schema_name, value)
    assert not validator.is_valid(value)

    value["expires_at"] = "2026-09-03T12:00:00.000000Z"
    assert not runtime.validate_instance(schema_name, value)
    assert validator.is_valid(value)


def test_completed_worker_result_forbids_failure_evidence() -> None:
    runtime = _load("schema_runtime")
    schemas, registry = _oracle()
    schema_name = "worker-execution-result-v1.schema.json"
    value = json.loads((FIXTURES / "valid/worker-execution-result-v1.json").read_text())
    value["errors"] = [
        {"code": "FAILED", "template_id": "failed", "field_path": "/", "parameters": []}
    ]
    validator = Draft202012Validator(schemas[schema_name], registry=registry)

    assert runtime.validate_instance(schema_name, value)
    assert not validator.is_valid(value)


def test_every_absolute_path_pattern_rejects_lexical_traversal() -> None:
    schemas, _ = _oracle()
    patterns: list[str] = []

    def collect(value: object) -> None:
        if isinstance(value, dict):
            mapping = cast(dict[str, object], value)
            pattern = mapping.get("pattern")
            if isinstance(pattern, str) and pattern.startswith("^/"):
                patterns.append(pattern)
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    collect(schemas)
    assert len(patterns) == 54
    for pattern in patterns:
        assert re.fullmatch(pattern, "/tmp/evidence.json")
        for attack in ("/../escape", "/./escape", "//escape", "/tmp/../escape"):
            assert re.fullmatch(pattern, attack) is None, (pattern, attack)
