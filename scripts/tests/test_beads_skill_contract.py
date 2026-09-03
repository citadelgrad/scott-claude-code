"""Contract tests for the Hermes-first Beads skill spine."""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = ROOT / "skills" / "beads"
SCRIPT = SKILL_ROOT / "scripts" / "beads_skill_contract.py"
REQUIRED_FILES = (
    "SKILL.md",
    "README.md",
    "LICENSE.txt",
    "references/sources.md",
    "scripts/beads_skill_contract.py",
)


def load_contract():
    spec = importlib.util.spec_from_file_location("beads_skill_contract", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["beads_skill_contract"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def contract():
    return load_contract()


def test_package_spine_contains_every_owned_runtime_artifact() -> None:
    missing = [path for path in REQUIRED_FILES if not (SKILL_ROOT / path).is_file()]
    assert missing == []


def test_committed_package_satisfies_static_contract(contract) -> None:
    report = contract.validate_package(SKILL_ROOT)
    assert report.errors == []
    assert report.preferred_drift == []
    assert 150 <= report.skill_lines <= 220
    assert report.estimated_tokens <= 2_500


def test_frontmatter_and_attribution_fail_closed(tmp_path: Path, contract) -> None:
    candidate = tmp_path / "beads"
    shutil.copytree(SKILL_ROOT, candidate)
    skill = candidate / "SKILL.md"
    skill.write_text(
        skill.read_text(encoding="utf-8")
        .replace("name: beads", "name: beads-rust")
        .replace("author: Scott Nixon", "author: Unknown"),
        encoding="utf-8",
    )
    sources = candidate / "references" / "sources.md"
    sources.write_text(
        sources.read_text(encoding="utf-8").replace(contract.BEADS_COMMIT, "0" * 40),
        encoding="utf-8",
    )

    errors = contract.validate_package(candidate).errors
    assert any("name must be beads" in error for error in errors)
    assert any("author must be Scott Nixon" in error for error in errors)
    assert any("Beads commit" in error for error in errors)


def test_frontmatter_rejects_duplicate_keys_and_non_string_metadata(
    tmp_path: Path, contract
) -> None:
    candidate = tmp_path / "beads"
    shutil.copytree(SKILL_ROOT, candidate)
    skill = candidate / "SKILL.md"
    original = skill.read_text(encoding="utf-8")
    skill.write_text(
        original.replace("name: beads\n", "name: beads\nname: duplicate\n", 1).replace(
            'version: "1.0.0"', "version: 1.0"
        ),
        encoding="utf-8",
    )

    errors = contract.validate_package(candidate).errors
    assert any("duplicate frontmatter key" in error for error in errors)
    assert any("metadata value must be a quoted string" in error for error in errors)


def test_frontmatter_enforces_agent_skills_character_limits(
    tmp_path: Path, contract
) -> None:
    candidate = tmp_path / "beads"
    shutil.copytree(SKILL_ROOT, candidate)
    skill = candidate / "SKILL.md"
    original = skill.read_text(encoding="utf-8")
    skill.write_text(
        original.replace(
            "description: >-\n",
            "description: " + ("x" * 1025) + "\n_original_description: >-\n",
            1,
        ).replace(
            "compatibility: >-\n",
            "compatibility: " + ("x" * 501) + "\n_original_compatibility: >-\n",
            1,
        ),
        encoding="utf-8",
    )

    errors = contract.validate_package(candidate).errors
    assert any("description exceeds 1024 characters" in error for error in errors)
    assert any("compatibility exceeds 500 characters" in error for error in errors)


def test_hard_budget_fails_and_preferred_budget_only_warns(
    tmp_path: Path, contract
) -> None:
    candidate = tmp_path / "beads"
    shutil.copytree(SKILL_ROOT, candidate)
    skill = candidate / "SKILL.md"
    original = skill.read_text(encoding="utf-8")

    skill.write_text(original + "\n" + "drift\n" * 15, encoding="utf-8")
    report = contract.validate_package(candidate)
    assert report.errors == []
    assert any("preferred line band" in warning for warning in report.preferred_drift)

    skill.write_text(original + "\n" + "overflow\n" * 501, encoding="utf-8")
    report = contract.validate_package(candidate)
    assert any("hard line limit" in error for error in report.errors)


def test_required_labels_and_one_hop_links_are_enforced(
    tmp_path: Path, contract
) -> None:
    candidate = tmp_path / "beads"
    shutil.copytree(SKILL_ROOT, candidate)
    skill = candidate / "SKILL.md"
    skill.write_text(
        skill.read_text(encoding="utf-8")
        .replace("Scope contract:", "Scope:")
        .replace("references/solo-execution.md", "references/routes/solo.md"),
        encoding="utf-8",
    )

    errors = contract.validate_package(candidate).errors
    assert any("Scope contract:" in error for error in errors)
    assert any("one hop" in error for error in errors)


def test_every_t04_operational_reference_is_linked_from_the_spine() -> None:
    skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    for reference in (
        "references/operating-modes.md",
        "references/issue-lifecycle.md",
        "references/verification-and-closure.md",
        "references/git-and-dolt-boundaries.md",
    ):
        assert f"]({reference})" in skill


def test_local_links_reject_absolute_paths_and_traversal(
    tmp_path: Path, contract
) -> None:
    candidate = tmp_path / "beads"
    shutil.copytree(SKILL_ROOT, candidate)
    skill = candidate / "SKILL.md"
    skill.write_text(
        skill.read_text(encoding="utf-8")
        + "\n[escape](../outside.md)\n[absolute](/tmp/outside.md)\n",
        encoding="utf-8",
    )

    errors = contract.validate_package(candidate).errors
    assert any("unsafe local link" in error for error in errors)


def test_orphaned_reference_files_are_rejected(tmp_path: Path, contract) -> None:
    candidate = tmp_path / "beads"
    shutil.copytree(SKILL_ROOT, candidate)
    (candidate / "references" / "unreachable.md").write_text(
        "# Unreachable\n", encoding="utf-8"
    )

    errors = contract.validate_package(candidate).errors
    assert any(
        "orphaned reference files" in error and "references/unreachable.md" in error
        for error in errors
    )


def test_reference_links_make_indirect_documents_reachable(
    tmp_path: Path, contract
) -> None:
    candidate = tmp_path / "beads"
    shutil.copytree(SKILL_ROOT, candidate)
    indirect = candidate / "references" / "indirect.md"
    indirect.write_text("# Indirect\n", encoding="utf-8")
    solo = candidate / "references" / "solo-execution.md"
    solo.write_text(
        solo.read_text(encoding="utf-8") + "\n[indirect](indirect.md)\n",
        encoding="utf-8",
    )

    errors = contract.validate_package(candidate).errors
    assert not any("orphaned reference files" in error for error in errors)


@pytest.mark.parametrize(
    "prompt",
    [
        "Implement bead scc-0pu.2",
        "Take the next ready task and claim it with bd",
        "Resume this tracked issue after compaction",
        "Create epic children and dependency ordering",
        "Delegate these independent Beads in parallel",
        "Track this human approval in Beads",
        "Explain this Beads issue without changing it",
    ],
)
def test_positive_discovery_corpus_triggers(prompt: str, contract) -> None:
    assert contract.discovery_decision(prompt, trusted_beads_repo=False)


@pytest.mark.parametrize(
    "prompt",
    [
        "What time is it?",
        "Explain this function without changing it",
        "Rewrite this sentence",
        "Brainstorm a plan without choosing a tracker",
        "Use Paperclip in this repository",
        "Author this PAS pipeline",
    ],
)
def test_negative_discovery_corpus_remains_quiet(prompt: str, contract) -> None:
    assert not contract.discovery_decision(prompt, trusted_beads_repo=False)


def test_trusted_beads_repo_triggers_only_nontrivial_tracked_mutation(contract) -> None:
    assert contract.discovery_decision(
        "Implement this nontrivial repository change", trusted_beads_repo=True
    )
    assert not contract.discovery_decision(
        "Explain this file without mutation", trusted_beads_repo=True
    )


def test_cli_reports_a_valid_portable_package() -> None:
    result = subprocess.run(
        [sys.executable, SCRIPT, "check", SKILL_ROOT],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Beads skill package contract OK" in result.stdout


def test_sources_pin_the_authoritative_corrected_baseline(contract) -> None:
    sources = (SKILL_ROOT / "references" / "sources.md").read_text(encoding="utf-8")
    assert contract.BASELINE_RESULTS_SHA256 == (
        "45875c97747278bd669e039ae71a1bdfca9ff8aa7da56694c5a7a3e6ff22fa44"
    )
    assert "15/18" in sources
    assert "12/18" not in sources
