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
preferred context budgets, and pinned attribution. Preferred budget drift is
reported separately from hard contract failures. It does not pretend to predict
Hermes's model-mediated routing.

## Verify actual Hermes discovery

`scripts/hermes_discovery_harness.py` installs a hash-bound copy into a
throwaway `HERMES_HOME`, with a separate `HOME`, and exercises the frozen Hermes
scanner plus `skill_view` handler without a model call:

```sh
CANDIDATE_SHA=$(uv run --python 3.12 python scripts/hermes_discovery_harness.py candidate-hash .)
uv run --python 3.12 python scripts/hermes_discovery_harness.py probe \
  --candidate . --candidate-sha256 "$CANDIDATE_SHA" \
  --hermes-source "$HOME/.hermes/hermes-agent" \
  --runtime-root /private/tmp/beads-discovery-probe \
  --default-home "$HOME/.hermes"
```

Automatic selection is an LLM decision and cannot be established by a keyword
oracle or explicit load. The `run` subcommand accepts the custodian's complete
72-scenario envelope, sends each undisclosed prompt through stdin (`--query-file
-`), records only `skill_view` and committed load counts plus hashes, deletes
every scenario home, and refuses to start without the exact authorization text
printed by `plan`. It caps the run at 72 isolated sessions, two provider
requests per session (144 maximum), and no paid API fallback.

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
