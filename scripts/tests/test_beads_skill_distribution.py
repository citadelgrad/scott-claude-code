"""Distribution and provenance contracts for the portable Beads skill."""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CANONICAL_SKILL = ROOT / "skills/beads/SKILL.md"
INSTALL_DOCS = (
    ROOT / "README.md",
    ROOT / "QUICK-START.md",
    ROOT / "docs/skills-cli.md",
    ROOT / "PUBLISHING.md",
)


def test_beads_has_one_canonical_installable_package() -> None:
    canonical = [
        path
        for path in (ROOT / "skills").glob("*/SKILL.md")
        if path.parent.name == "beads"
    ]
    plugin_copies = list((ROOT / "plugins").glob("**/beads/SKILL.md"))

    assert canonical == [CANONICAL_SKILL]
    assert plugin_copies == []


def test_beads_is_grouped_once_in_portable_manifest() -> None:
    manifest = json.loads((ROOT / "skills.sh.json").read_text(encoding="utf-8"))
    grouped = [
        name for grouping in manifest["groupings"] for name in grouping["skills"]
    ]

    assert grouped.count("beads") == 1


def test_authorship_preserves_local_adaptation_and_upstream_provenance() -> None:
    authorship = (ROOT / "SKILL-AUTHORSHIP.md").read_text(encoding="utf-8")

    assert authorship.count("- `skills/beads/SKILL.md`") == 1
    assert "Scott Nixon" in authorship
    assert "Beads Contributors" in authorship
    assert "gastownhall/beads" in authorship
    assert "skills/beads/LICENSE.txt" in authorship
    assert "skills/beads/references/sources.md" in authorship


def test_catalogs_and_install_docs_publish_beads_and_current_counts() -> None:
    hardened = (ROOT / "docs/hardened-skills.md").read_text(encoding="utf-8")
    assert "`beads`" in hardened
    assert "[Beads](../skills/beads/README.md)" in hardened

    for path in INSTALL_DOCS:
        text = path.read_text(encoding="utf-8")
        assert "beads" in text.lower(), path
        assert "npx skills add citadelgrad/scott-cc" in text, path

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    quick_start = (ROOT / "QUICK-START.md").read_text(encoding="utf-8")
    skills_cli = (ROOT / "docs/skills-cli.md").read_text(encoding="utf-8")
    publishing = (ROOT / "PUBLISHING.md").read_text(encoding="utf-8")
    authorship = (ROOT / "SKILL-AUTHORSHIP.md").read_text(encoding="utf-8")

    assert "## Skills (30)" in readme
    assert re.search(r"\*\*30 skills\*\*", readme)
    assert "- 30 skills" in quick_start
    assert "30 core skills" in skills_cli
    assert "30 skills + beads epic builder" in publishing
    assert "73 installable `SKILL.md` files" in authorship
