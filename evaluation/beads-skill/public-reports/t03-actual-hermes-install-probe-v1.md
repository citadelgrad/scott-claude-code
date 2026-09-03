# T03 actual-Hermes discovery evidence

## No-model evidence

The adjacent JSON report was produced by Hermes Agent commit
`21b2095d00a98b8ad7b5c60b10587619c852cdb8`. The harness copied candidate tree
SHA-256 `1c634d17e4e44b9fa25670360d33492d774e27af2048e6480450191c2a3c1c99`
into a throwaway `HERMES_HOME`. Frozen Hermes then:

- discovered `beads` through its real installed-skill scanner;
- loaded it through the real `skill_view` tool handler;
- committed one isolated-profile load event;
- retained no skill body, model output, hidden prompt, or sealed prompt;
- ran under a macOS sandbox that was behaviorally proven to deny writes beneath
  `/Users/scott/.hermes`.

The concurrent default profile's byte snapshot changed while the outer Hermes
session was recording this tool invocation, so the JSON reports
`default_profile_unchanged: false`. This is not attributed to the probe child:
the child had no kernel write capability for that tree, as recorded by
`default_profile_write_denied: true` and
`default_profile_mutation_by_harness: false`.

## Remaining AC-T03-001 blocker

Automatic skill selection is model-mediated. Existing recorded baseline sessions
were generated with the no-skill or upstream treatments, not this candidate, so
replaying them cannot establish candidate discovery. The disconnected keyword
oracle was removed rather than presented as Hermes evidence. No model run was
executed during this remediation.

Approval needed: **72 isolated Hermes sessions**, `openai-codex` /
`gpt-5.6-sol`, at most **2 provider requests per session (144 total)**, a
**180-second wall-clock cap per session**, OpenAI Codex subscription quota, and
**no paid API fallback**.

Required authorization text:

```text
authorize 72 candidate discovery runs via openai-codex/gpt-5.6-sol
```

After the benchmark custodian supplies the private 72-scenario envelope and its
independently recorded whole-envelope SHA-256, run:

```sh
uv run --python 3.12 python skills/beads/scripts/hermes_discovery_harness.py run \
  --candidate skills/beads \
  --candidate-sha256 1c634d17e4e44b9fa25670360d33492d774e27af2048e6480450191c2a3c1c99 \
  --corpus "$PRIVATE_ROUTING_CORPUS" \
  --corpus-sha256 "$PRIVATE_ROUTING_CORPUS_SHA256" \
  --design docs/plans/2026-09-02-hermes-beads-skill/benchmark-corpus-design-v1.json \
  --hermes-source /Users/scott/.hermes/hermes-agent \
  --runtime-root /private/tmp/scc-0pu-2-routing-run \
  --default-home /Users/scott/.hermes \
  --credentials "$ISOLATED_CREDENTIAL_TEMPLATE" \
  --provider openai-codex --model gpt-5.6-sol \
  --authorization 'authorize 72 candidate discovery runs via openai-codex/gpt-5.6-sol' \
  --output evaluation/beads-skill/public-reports/t03-routing-v1.json
```

The command sends prompt bodies only over stdin, stores raw sessions only inside
each throwaway profile, records sanitized event counts and hashes, and deletes
each scenario home before continuing.
