# Sources and attribution

This file pins the inputs used to author the local `beads` skill spine. Local
changes add Hermes-first routing, trigger restraint, parent-only lifecycle
mutation, guarded readback, capability-gated completion, and honest uncertainty.
They are not claims about newer upstream releases.

## Beads

- Project: [`gastownhall/beads`](https://github.com/gastownhall/beads)
- Version: `1.2.2`
- Tag object: `8ed120b1b3afdb75345719c2d3fef07a81860ab8`
- Immutable commit: [`6c124203e771433a3550c348771a5b5e27fd3c21`](https://github.com/gastownhall/beads/commit/6c124203e771433a3550c348771a5b5e27fd3c21)
- Upstream skill: [`plugins/beads/skills/beads/SKILL.md`](https://raw.githubusercontent.com/gastownhall/beads/6c124203e771433a3550c348771a5b5e27fd3c21/plugins/beads/skills/beads/SKILL.md)
- License: [MIT at the pinned commit](https://raw.githubusercontent.com/gastownhall/beads/6c124203e771433a3550c348771a5b5e27fd3c21/LICENSE)
- Copyright: Copyright (c) 2025 Beads Contributors
- Pinned upstream skill SHA-256: `01555fe65d19be401d820d9dec029cd048fb0791d433b4b575374477d6f1d827`

The local skill substantially adapts upstream workflow guidance. Its full MIT
permission notice and copyright are retained in `../LICENSE.txt`. Runtime CLI
syntax is deliberately not copied: `bd prime` and `bd <command> --help` are the
live source of truth. The skill targets `bd`, not `br`/beads_rust.

## Hermes Agent

- Project: [`NousResearch/hermes-agent`](https://github.com/NousResearch/hermes-agent)
- Current documentation: <https://hermes-agent.nousresearch.com/docs>
- Approved package snapshot: [`21b2095d00a98b8ad7b5c60b10587619c852cdb8`](https://github.com/NousResearch/hermes-agent/tree/21b2095d00a98b8ad7b5c60b10587619c852cdb8)
- Observed package version: `0.21.0` (`2026.8.31`)
- License: [MIT at the pinned snapshot](https://raw.githubusercontent.com/NousResearch/hermes-agent/21b2095d00a98b8ad7b5c60b10587619c852cdb8/LICENSE)
- Copyright: Copyright (c) 2025 Nous Research

This identity is an approved post-release package snapshot, not the commit
pointed to by the `v2026.8.31` tag. Hermes supplies isolated conversation
contexts and model-facing delegation; it does not by itself guarantee isolated
writable filesystems or restart-surviving execution.

Discovery verification additionally follows the pinned implementation in
[`agent/prompt_builder.py`](https://github.com/NousResearch/hermes-agent/blob/21b2095d00a98b8ad7b5c60b10587619c852cdb8/agent/prompt_builder.py),
[`model_tools.py`](https://github.com/NousResearch/hermes-agent/blob/21b2095d00a98b8ad7b5c60b10587619c852cdb8/model_tools.py), and
[`tools/skills_tool.py`](https://github.com/NousResearch/hermes-agent/blob/21b2095d00a98b8ad7b5c60b10587619c852cdb8/tools/skills_tool.py).
The scanner and loader are deterministic local code; choosing whether to call
`skill_view` from task text is model-mediated and requires a model run.

## Agent Skills specification

- Specification: <https://agentskills.io/specification>
- Immutable snapshot: [`69ef37e9424c0a7ea9dd2293b559e43ec8176379`](https://raw.githubusercontent.com/agentskills/agentskills/69ef37e9424c0a7ea9dd2293b559e43ec8176379/docs/specification.mdx)
- Documentation license: [CC-BY-4.0](https://creativecommons.org/licenses/by/4.0/)
- Code license: Apache-2.0
- Origin: the format was originally developed by Anthropic and released as an
  open standard.

The local frontmatter, one-hop progressive disclosure, and approximate context
budgets adapt this specification. No Agent Skills validator code is copied.

## Frozen local evidence

The three predecessor artifacts were read from
`docs/plans/2026-09-02-hermes-beads-skill/` in the primary repository and
verified byte-for-byte with SHA-256 before authoring:

| Artifact | SHA-256 |
|---|---|
| `source-baseline-v1.json` | `24b39b6f8e75af3b2f09becc1af2d10a59a92a2af8222e6caeec3d4eaa1681c0` |
| `benchmark-corpus-design-v1.json` | `29963dcb172bd3d7030c45eda797959a5341b9c0067c168c7340e1f2dc454734` |
| `baseline-results-v1.json` | `a894f1f935de1bee789f8c06faee8c7574fa3b6d1157be51d8739736ffe260df` |

The baseline decision was **proceed**: no-skill procedural compliance was
`3/18`; the pinned upstream skill achieved `15/18`. These results motivate a
candidate rather than proving candidate quality. Hidden and sealed corpus
contents remain outside this package. A deterministic post-hoc canary probe
found no identity matches on preserved prompts, copied filesystems, tool-event
arguments/results, or model-visible outputs. It fails closed overall because
the 36 preserved session rows contain no system-context bytes; comprehensive
process/network telemetry was also not captured. Candidate authoring therefore
remains blocked on the leakage-boundary acceptance criterion despite the
performance-only proceed decision.

Sources were verified on `2026-09-03`. On source drift, preserve this immutable
baseline, compare current authoritative documentation and live command help,
and create a reviewed versioned update; never silently rewrite pinned claims.
