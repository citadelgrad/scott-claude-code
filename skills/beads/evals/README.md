# Beads skill evaluation

This directory contains only the frozen **public development** split. It is illustrative and does not gate release. Validation and sealed-test prompts, expected states, rubrics, sibling fixtures, canaries, and raw outputs stay in access-controlled external stores and must never be copied here or mounted into an evaluated agent.

## Immutable inputs

`public-dev/corpus-v1.json` copies the 24 public rows, budget profiles, observable assertions, and scoring contract from `benchmark-corpus-design-v1.json`. Its content identity is recovered exactly with:

```text
sha256(canonical compact sorted-key JSON + LF of
  {schema, public, budget_profiles, observable_assertions, scoring})
= 03be9aafd2e39605e7a31d82bedff9d5d24061d7b2dddb1986fa78fa26b975c6
```

`evaluation/beads-skill/manifests/release-v1.json` pins predecessor file hashes, the public/private split identities, the complete frozen budgets, thresholds, release treatments, local-gap IDs, and a repository-safe scenario index. Private rows in that index contain only ID, split, category, polarity, budget profile, and coverage tags.

Validate the public contract without contacting a provider:

```bash
uv run --python 3.12 python skills/beads/scripts/evaluate_skill.py validate-corpus \
  --repo-root . \
  --corpus skills/beads/evals/public-dev/corpus-v1.json \
  --manifest evaluation/beads-skill/manifests/release-v1.json
```

## Custodian workflow

1. Outside the candidate-visible filesystem, clone one pristine Git and Beads fixture for each treatment with `prepare_pair`. Allocate distinct workspace, home, skill registry, session, actor, run-root, cache, and process namespaces.
2. Bind task, model, provider, harness/version, tools, budget, and pre-state hashes in `pair_identity`. Treatment order must be randomized or counterbalanced; caches never cross treatments.
3. Mount only the candidate skill and one cloned task workspace. Never mount this scorer, expected state, external corpus roots, or sibling fixtures.
4. Capture schema-valid worker execution results, coordinator run checkpoints, and operation journals outside model context. Raw model reasoning, assistant claims, echoed commands, worker prose, secrets, and raw stdout/stderr are not accepted evidence.
5. Adapt runtime artifacts with `adapt-evidence`. The versioned adapter (`beads.evidence-adapter.v1`) is the only sanctioned translation into typed events and deterministic outcome checks; bespoke standalone translations are rejected.
6. Run `score-run`, then `compare-pair`. Every scored event, result class, and outcome hash is re-derived from the authenticated capture evidence bound to worker/coordinator run identities; caller-authored labels fail closed. Quarantine any pre-state mismatch, canary hit, path alias, binding drift, or missing treatment; do not score it.
7. Aggregate only pair-validated records passed via the required `--records` immutable pair receipts; `check_thresholds` fails closed on empty records, and every aggregate is recomputed through `aggregate_results` from the positional records. Infrastructure-invalid runs are counted by treatment/reason and rerun within the frozen cap. Missing telemetry is excluded only from its numeric distribution, never behavioral denominators. Infrastructure-invalid runs are counted by treatment/reason and rerun within the frozen cap. Missing telemetry is excluded only from its numeric distribution, never behavioral denominators.
8. Run `check-thresholds` with the aggregate report, the manifest, and the immutable pair receipts. The report must recompute byte-for-byte from the receipts; a nonzero frozen release matrix, unblocked manifest status, and at least one record are required. Exit 3 means release gates failed; validation/contamination errors exit 2. A high aggregate never overrides a hard gate.

`score-run` consumes a closed custodian record containing evaluation-result identities, polarity, required assertion IDs, forbidden-action IDs, `capture_evidence` (schema-valid worker execution result, coordinator run checkpoint, operation journal, and the adapter-derived evidence bundle), telemetry, and the exact frozen budget profile. It emits `beads.evaluation-result.v1` and validates it against the committed schema.

`aggregate` emits canonical JSON plus deterministic Markdown. Reports include exact micro denominators, unweighted family macros, Wilson lower bounds, exclusions, variance, median/p95/worst telemetry, absolute and incremental paired lift, local-gap lift, and pinned provenance.

## Semantic judge boundary

A semantic judge is optional and reporting-only for explanation clarity. `validate_semantic_judgment` accepts at most eight blinded rubric items and 4 KiB. Unavailability is reported as unavailable. Treatment labels in a blinded ID fail closed, and semantic output cannot alter command order, identity, state, safety, outcome, or hard-gate scores.

## No live evaluation in this package change

The checked-in public report is an explicit zero-run/not-evaluated artifact. Running live candidate, hidden-validation, sealed-test, alternative-provider, subscription-quota, or paid evaluation requires separate authorization and the external custodian environment.
