from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[3]
FINALIZE_TEST = ROOT / "scripts/tests/beads_worker_result/test_finalize.py"
VALIDATOR = ROOT / "skills/beads/scripts/validate_worker_execution_result.py"


def _load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_validator_binds_exact_result_and_receipt_and_rejects_stale_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixtures = _load(FINALIZE_TEST, "worker_result_validation_fixtures")
    worker, context, candidate = fixtures._setup(tmp_path, monkeypatch)
    descriptor = fixtures._descriptor(tmp_path, [])
    worker.finalize_attempt(context, candidate, sensitive_values_file=descriptor)
    validator = _load(VALIDATOR, "beads_worker_result_validator")

    accepted = validator.validate_finalized_attempt(
        result_path=context.outbox / "result.json",
        receipt_path=context.outbox / "receipt.json",
        packet=context.packet,
        ownership_epoch=context.ownership_epoch,
    )
    assert accepted.value["status"] == "completed"

    receipt = json.loads((context.outbox / "receipt.json").read_text())
    receipt["ownership_epoch"] += 1
    (context.outbox / "receipt.json").write_text(
        json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n"
    )
    with pytest.raises(
        validator.WorkerExecutionResultError, match="RECEIPT_IDENTITY_MISMATCH"
    ):
        validator.validate_finalized_attempt(
            result_path=context.outbox / "result.json",
            receipt_path=context.outbox / "receipt.json",
            packet=context.packet,
            ownership_epoch=context.ownership_epoch,
        )
