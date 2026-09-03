# Beads — Hermes-first durable issue coordination

This package is a local Agent Skill for using Go/Dolt Beads (`bd`) safely in
solo work and bounded Hermes coordination. The compact `SKILL.md` owns product
boundary, route precedence, invariants, STOP conditions, and honest completion.
Detailed operational routes are progressively disclosed through direct
`references/` links as later package stages land.

## What the spine guarantees

- It triggers for explicit Beads or trusted tracked-lifecycle work and stays
  quiet for unrelated or ephemeral tasks.
- Native `bd`, `bd prime`, and live command help remain authoritative.
- It never initializes a workspace automatically or accesses Dolt tables.
- A swarm parent is the only Beads lifecycle writer; child claims require
  independent verification before acceptance.
- Completion requires available, affirmative, target-bound evidence and exact
  readback. Missing evidence remains blocked or inconclusive.

## Validate the package

```sh
python3 scripts/beads_skill_contract.py check .
```

The checker is Python-standard-library only. It validates the package spine,
frontmatter, required orchestration labels, direct-reference topology, hard and
preferred context budgets, discovery corpus, and pinned attribution. Preferred
budget drift is reported separately from hard contract failures.

## Scope

This t03 package contains the spine, package documentation, license notices,
source attribution, and static checker. Behavioral references and runtime
schemas/coordinator helpers are intentionally owned by later implementation
tasks; this stage does not fabricate those mechanics in prose.

## License and provenance

The local package is MIT licensed by Scott Nixon. It adapts the upstream Beads
skill and workflow concepts from `gastownhall/beads`; the upstream copyright,
MIT notice, immutable commit, and links are retained in `LICENSE.txt` and
[`references/sources.md`](references/sources.md). Hermes Agent and Agent Skills
specification provenance are also pinned there.
