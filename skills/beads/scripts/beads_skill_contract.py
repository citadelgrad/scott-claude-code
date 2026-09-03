#!/usr/bin/env python3
"""Validate the static Hermes-first Beads skill package spine."""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

BEADS_COMMIT = "6c124203e771433a3550c348771a5b5e27fd3c21"
HERMES_COMMIT = "21b2095d00a98b8ad7b5c60b10587619c852cdb8"
AGENT_SKILLS_COMMIT = "69ef37e9424c0a7ea9dd2293b559e43ec8176379"
SOURCE_BASELINE_SHA256 = (
    "24b39b6f8e75af3b2f09becc1af2d10a59a92a2af8222e6caeec3d4eaa1681c0"
)
CORPUS_SHA256 = "29963dcb172bd3d7030c45eda797959a5341b9c0067c168c7340e1f2dc454734"
BASELINE_RESULTS_SHA256 = (
    "a894f1f935de1bee789f8c06faee8c7574fa3b6d1157be51d8739736ffe260df"
)

REQUIRED_FILES = (
    "SKILL.md",
    "README.md",
    "LICENSE.txt",
    "references/sources.md",
    "scripts/beads_skill_contract.py",
    "scripts/hermes_discovery_harness.py",
)
REQUIRED_LABELS = (
    "Scope contract:",
    "Fan-out contract:",
    "Artifact contract:",
    "Failure contract:",
    "Continuation contract:",
    "Mechanical-test contract:",
)
REQUIRED_LINKS = (
    "references/operating-modes.md",
    "references/solo-execution.md",
    "references/issue-lifecycle.md",
    "references/verification-and-closure.md",
    "references/git-and-dolt-boundaries.md",
    "references/hermes-swarm.md",
    "references/worker-contract.md",
    "references/issue-quality.md",
    "references/dependencies-and-ready-fronts.md",
    "references/workspace-and-health.md",
    "references/recovery-and-resume.md",
    "references/human-and-async-gates.md",
    "references/pas-comparison.md",
    "references/troubleshooting.md",
    "references/sources.md",
)
REQUIRED_SPINE_TEXT = (
    "Never automatically run `bd init`",
    "parent/coordinator is the sole Beads lifecycle writer",
    "exact readback",
    "Completion is capability-gated",
    "`blocked`",
    "`inconclusive`",
    "bd prime",
    "bd <command> --help",
)
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_]+|[^\w\s]", re.UNICODE)
MARKDOWN_LINK_PATTERN = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
BEAD_ID_PATTERN = re.compile(r"\b[a-z][a-z0-9]*-[a-z0-9]+(?:\.\d+)?\b", re.IGNORECASE)


@dataclass(frozen=True)
class ContractReport:
    """Static validation results with hard errors separate from soft drift."""

    errors: list[str]
    preferred_drift: list[str]
    skill_lines: int
    estimated_tokens: int


def _frontmatter(text: str) -> tuple[dict[str, str], dict[str, str]]:
    """Parse the small Agent Skills frontmatter subset used by this package."""
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        return {}, {}
    block = text.split("\n---\n", 1)[0][4:]
    lines = block.splitlines()
    top: dict[str, str] = {}
    metadata: dict[str, str] = {}
    index = 0
    in_metadata = False
    while index < len(lines):
        line = lines[index]
        if line == "metadata:":
            in_metadata = True
            index += 1
            continue
        if not line.strip() or ":" not in line:
            index += 1
            continue
        indent = len(line) - len(line.lstrip())
        key, raw_value = line.strip().split(":", 1)
        if in_metadata and indent == 0:
            in_metadata = False
        value = raw_value.strip().strip("\"'")
        if value in {">", ">-", "|", "|-"}:
            continuation: list[str] = []
            index += 1
            while index < len(lines) and (
                not lines[index].strip()
                or len(lines[index]) - len(lines[index].lstrip()) > indent
            ):
                if lines[index].strip():
                    continuation.append(lines[index].strip())
                index += 1
            value = " ".join(continuation)
            target = metadata if in_metadata and indent > 0 else top
            target[key] = value
            continue
        target = metadata if in_metadata and indent > 0 else top
        target[key] = value
        index += 1
    return top, metadata


def _frontmatter_structure_errors(text: str) -> list[str]:
    """Reject duplicate keys and scalar forms YAML would coerce from strings."""
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        return ["SKILL.md requires a complete YAML frontmatter block"]
    block = text.split("\n---\n", 1)[0][4:]
    errors: list[str] = []
    seen_top: set[str] = set()
    seen_metadata: set[str] = set()
    in_metadata = False
    for line in block.splitlines():
        if not line.strip() or ":" not in line:
            continue
        indent = len(line) - len(line.lstrip())
        key, raw_value = line.strip().split(":", 1)
        if indent == 0:
            in_metadata = key == "metadata"
            if key in seen_top:
                errors.append(f"duplicate frontmatter key: {key}")
            seen_top.add(key)
            continue
        if not in_metadata:
            continue
        if key in seen_metadata:
            errors.append(f"duplicate frontmatter metadata key: {key}")
        seen_metadata.add(key)
        value = raw_value.strip()
        if re.fullmatch(
            r"(?:[-+]?\d+(?:\.\d+)?|true|false|null|~|\[.*\]|\{.*\})",
            value,
            re.IGNORECASE,
        ):
            errors.append(
                "metadata value must be a quoted string when YAML would coerce it: "
                f"{key}"
            )
    return errors


def _validate_frontmatter(text: str) -> list[str]:
    errors = _frontmatter_structure_errors(text)
    top, metadata = _frontmatter(text)
    if top.get("name") != "beads":
        errors.append("frontmatter name must be beads")
    description = top.get("description", "")
    if not description.startswith("Use when"):
        errors.append("frontmatter description must start with 'Use when'")
    for term in ("bd/Beads", "nontrivial", "unrelated", "ephemeral"):
        if term not in description:
            errors.append(f"frontmatter description must discriminate {term!r}")
    if len(description) > 1_024:
        errors.append("frontmatter description exceeds 1024 characters")
    if top.get("license") != "MIT; see LICENSE.txt and references/sources.md":
        errors.append("frontmatter license must identify MIT and attribution files")
    if "Hermes-first" not in top.get("compatibility", ""):
        errors.append("frontmatter compatibility must be Hermes-first")
    if len(top.get("compatibility", "")) > 500:
        errors.append("frontmatter compatibility exceeds 500 characters")
    if metadata.get("author") != "Scott Nixon":
        errors.append("frontmatter author must be Scott Nixon")
    if metadata.get("version") != "1.0.0":
        errors.append("frontmatter version must be 1.0.0")
    if metadata.get("upstream") != "github.com/gastownhall/beads":
        errors.append("frontmatter upstream must credit github.com/gastownhall/beads")
    if not all(isinstance(value, str) for value in metadata.values()):
        errors.append("frontmatter metadata values must all be strings")
    return errors


def _reachable_reference_paths(root: Path, skill: str) -> set[Path]:
    """Return package-local Markdown references reachable from SKILL.md."""
    reference_root = (root / "references").resolve()
    pending: list[tuple[Path, str]] = [(root / "SKILL.md", skill)]
    visited_documents: set[Path] = set()
    reachable: set[Path] = set()

    while pending:
        source, text = pending.pop()
        source = source.resolve()
        if source in visited_documents:
            continue
        visited_documents.add(source)
        for raw_link in MARKDOWN_LINK_PATTERN.findall(text):
            link = raw_link.split("#", 1)[0].split("?", 1)[0]
            if not link or "://" in link or link.startswith("/"):
                continue
            candidate = (source.parent / link).resolve()
            if candidate.suffix.lower() != ".md" or not candidate.is_file():
                continue
            if candidate != reference_root and reference_root not in candidate.parents:
                continue
            relative = candidate.relative_to(root.resolve())
            if relative not in reachable:
                reachable.add(relative)
                pending.append((candidate, candidate.read_text(encoding="utf-8")))
    return reachable


def validate_package(skill_root: Path) -> ContractReport:
    """Validate a package root without requiring later-stage runtime artifacts."""
    root = Path(skill_root)
    errors: list[str] = []
    drift: list[str] = []
    missing = [path for path in REQUIRED_FILES if not (root / path).is_file()]
    errors.extend(f"missing required package file: {path}" for path in missing)
    skill_path = root / "SKILL.md"
    if not skill_path.is_file():
        return ContractReport(errors, drift, 0, 0)

    skill = skill_path.read_text(encoding="utf-8")
    line_count = len(skill.splitlines())
    token_count = len(TOKEN_PATTERN.findall(skill))
    if line_count > 500:
        errors.append(f"SKILL.md exceeds hard line limit: {line_count} > 500")
    elif not 150 <= line_count <= 220:
        drift.append(f"SKILL.md is outside preferred line band 150-220: {line_count}")
    if token_count > 5_000:
        errors.append(
            f"SKILL.md exceeds approximate hard token limit: {token_count} > 5000"
        )
    elif token_count > 2_500:
        drift.append(
            f"SKILL.md exceeds preferred approximate token budget: {token_count} > 2500"
        )

    errors.extend(_validate_frontmatter(skill))
    for label in REQUIRED_LABELS:
        if label not in skill:
            errors.append(f"SKILL.md missing required label: {label}")
    for required in REQUIRED_SPINE_TEXT:
        if required not in skill:
            errors.append(f"SKILL.md missing required spine text: {required}")

    links = MARKDOWN_LINK_PATTERN.findall(skill)
    for required in REQUIRED_LINKS:
        if required not in links:
            errors.append(f"SKILL.md missing direct operational link: {required}")
    nested = sorted(
        link
        for link in links
        if link.startswith("references/") and len(Path(link).parts) != 2
    )
    if nested:
        errors.append("reference links must remain one hop: " + ", ".join(nested))
    unsafe = []
    for link in links:
        if "://" in link or link.startswith("#"):
            continue
        path = Path(link)
        if path.is_absolute() or ".." in path.parts:
            unsafe.append(link)
            continue
        candidate = root / path
        if candidate.exists() and root.resolve() not in candidate.resolve().parents:
            unsafe.append(link)
    if unsafe:
        errors.append("unsafe local link: " + ", ".join(sorted(unsafe)))

    all_references = {
        path.resolve().relative_to(root.resolve())
        for path in (root / "references").glob("*.md")
        if path.is_file()
    }
    orphaned = sorted(all_references - _reachable_reference_paths(root, skill))
    if orphaned:
        errors.append(
            "orphaned reference files unreachable from SKILL.md: "
            + ", ".join(path.as_posix() for path in orphaned)
        )

    if len(re.findall(r"(?m)^\s*(?:[-*]|\d+\.)?\s*`?bd [^`\n]+", skill)) > 20:
        errors.append("SKILL.md appears to duplicate the bd CLI manual")

    sources_path = root / "references" / "sources.md"
    sources = sources_path.read_text(encoding="utf-8") if sources_path.is_file() else ""
    for label, value in (
        ("Beads commit", BEADS_COMMIT),
        ("Hermes commit", HERMES_COMMIT),
        ("Agent Skills commit", AGENT_SKILLS_COMMIT),
        ("source baseline hash", SOURCE_BASELINE_SHA256),
        ("corpus hash", CORPUS_SHA256),
        ("baseline results hash", BASELINE_RESULTS_SHA256),
    ):
        if value not in sources:
            errors.append(f"sources.md missing pinned {label}: {value}")
    for required in ("3/18", "15/18", "CC-BY-4.0", "Beads Contributors"):
        if required not in sources:
            errors.append(f"sources.md missing attribution/evidence text: {required}")

    license_path = root / "LICENSE.txt"
    license_text = (
        license_path.read_text(encoding="utf-8") if license_path.is_file() else ""
    )
    for required in (
        "MIT License",
        "Scott Nixon",
        "Copyright (c) 2025 Beads Contributors",
        "Permission is hereby granted",
    ):
        if required not in license_text:
            errors.append(f"LICENSE.txt missing required notice: {required}")

    readme_path = root / "README.md"
    readme = readme_path.read_text(encoding="utf-8") if readme_path.is_file() else ""
    for required in ("Hermes-first", "gastownhall/beads", "beads_skill_contract.py"):
        if required not in readme:
            errors.append(f"README.md missing package identity: {required}")

    return ContractReport(errors, drift, line_count, token_count)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    check = subparsers.add_parser("check", help="validate a Beads skill package")
    check.add_argument(
        "skill_root",
        nargs="?",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report = validate_package(args.skill_root)
    for warning in report.preferred_drift:
        print(f"DRIFT: {warning}")
    if report.errors:
        for error in report.errors:
            print(f"FAIL: {error}", file=sys.stderr)
        return 1
    print(
        "Beads skill package contract OK "
        f"(lines={report.skill_lines}, estimated_tokens={report.estimated_tokens})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
