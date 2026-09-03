from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "skills" / "beads" / "scripts"
SCHEMAS = ROOT / "skills" / "beads" / "schemas"
EXPECTED = {
    "approval-record-v1.schema.json",
    "checkpoint-pointer-v1.schema.json",
    "direct-operation-record-v1.schema.json",
    "direct-operation-request-v1.schema.json",
    "durable-executor-result-v1.schema.json",
    "durable-handoff-v1.schema.json",
    "evaluation-result-v1.schema.json",
    "harness-receipt-v1.schema.json",
    "lane-freeze-v1.schema.json",
    "native-command-event-v1.schema.json",
    "operation-journal-event-v1.schema.json",
    "operation-result-v1.schema.json",
    "ownership-history-event-v1.schema.json",
    "ownership-record-v1.schema.json",
    "parent-verification-v1.schema.json",
    "pending-action-v1.schema.json",
    "recovery-probe-v1.schema.json",
    "reviewer-result-v1.schema.json",
    "run-checkpoint-v1.schema.json",
    "run-manifest-v1.schema.json",
    "run-request-v1.schema.json",
    "safe-command-result-v1.schema.json",
    "worker-execution-result-v1.schema.json",
    "worker-packet-v1.schema.json",
}


def _load(name: str):
    path = SCRIPTS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"beads_{name}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_schema_inventory_and_generated_runtime_public_api() -> None:
    assert {path.name for path in SCHEMAS.glob("*.schema.json")} == EXPECTED
    runtime = _load("schema_runtime")
    assert (
        runtime.validate_instance(
            "checkpoint-pointer-v1.schema.json",
            {
                "schema_version": "beads.checkpoint-pointer.v1",
                "run_id": "run-0123456789abcdef-20260903T120000.000000Z-ABCDEFGH",
                "generation": 1,
                "generation_path": ".hermes/beads-runs/x/checkpoints/000001.json",
                "generation_sha256": "a" * 64,
            },
        )
        == []
    )
    assert runtime.validate_instance(
        "checkpoint-pointer-v1.schema.json",
        {
            "schema_version": "beads.checkpoint-pointer.v1",
            "run_id": "run-0123456789abcdef-20260903T120000.000000Z-ABCDEFGH",
            "generation": 1,
            "generation_path": "../escape",
            "generation_sha256": "a" * 64,
            "extra": True,
        },
    )
