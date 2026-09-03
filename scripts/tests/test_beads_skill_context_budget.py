"""Mechanical context-budget tests for the Beads orchestrator spine."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKILL_PATH = "skills/beads/SKILL.md"
SKILL = ROOT / SKILL_PATH
TOKEN_PATTERN = re.compile(r"\w+|[^\w\s]", re.UNICODE)


def skill_text() -> str:
    return SKILL.read_text(encoding="utf-8")


def test_preferred_context_budget_is_220_lines_and_2500_tokens() -> None:
    value = skill_text()

    assert len(value.splitlines()) <= 220
    assert len(TOKEN_PATTERN.findall(value)) <= 2_500


def test_hard_context_budget_is_500_lines_and_5000_tokens() -> None:
    value = skill_text()

    assert len(value.splitlines()) <= 500
    assert len(TOKEN_PATTERN.findall(value)) <= 5_000


def test_orchestration_contract_fields_are_declared() -> None:
    value = skill_text()

    for field in (
        "Scope contract:",
        "Fan-out contract:",
        "Artifact contract:",
        "Failure contract:",
        "Continuation contract:",
        "Mechanical-test contract:",
    ):
        assert field in value
