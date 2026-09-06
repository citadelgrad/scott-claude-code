"""Shared dynamic-load and fixture helpers for beads_dag tests.

Mirrors ``scripts/tests/beads_front/_common.py``'s dynamic-load pattern
(distinct ``sys.modules`` key per load so this suite's imports of the same
production files never collide with a sibling suite's).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "skills/beads/scripts"
FIXTURES = ROOT / "scripts/tests/fixtures/beads_dag"

_counter = {"n": 0}


def load(name: str):
    _counter["n"] += 1
    path = SCRIPTS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(
        f"beads_dag_{_counter['n']}_{name}", path
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def static_fixture_paths() -> list[Path]:
    return sorted((FIXTURES / "static").glob("*.json"))


def dynamic_fixture_paths() -> list[Path]:
    return sorted((FIXTURES / "dynamic").glob("*.json"))


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def build_snapshot(step: dict[str, Any]) -> dict[str, Any]:
    """Project one fixture (or one dynamic-fixture step) into a snapshot dict.

    Shape matches ``scripts/tests/beads_front/_common.py``'s ``_snapshot()``
    helper, which is what ``coordinator_front.build_front_from_snapshot``
    (t11, the frozen predecessor) expects.
    """
    return {
        "schema_version": "beads.issue-snapshot.v1",
        "captured_at": "2026-09-04T00:00:00.000000Z",
        "workspace_sha256": "0" * 64,
        "cli_version": "1.2.2",
        "root_issue_id": step.get("root_issue_id"),
        "ready": step["ready"],
        "blocked": step.get("blocked", []),
        "cycles": step.get("cycles", {"cycles": [], "count": 0}),
        "gates": step.get("gates", []),
        "human": [],
    }


def evaluate_step(module, step: dict[str, Any], *, repository_root: str):
    """Run one fixture/step through the real predecessor contract.

    Returns the ``FrontResult`` from ``coordinator_front.build_front_from_snapshot``.
    """
    snapshot = build_snapshot(step)
    return module.build_front_from_snapshot(
        snapshot,
        repository_root=repository_root,
        hard_cap=step["hard_cap"],
        declared_edges=step.get("declared_edges"),
        consistency=step.get("consistency"),
        live_ready_order=step.get("live_ready_order", True),
    )


def excluded_reasons(front) -> dict[str, str]:
    return {entry["id"]: entry["reason"] for entry in front.excluded}
