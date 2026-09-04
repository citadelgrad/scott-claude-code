# Evaluator implementation boundary

`evaluator.py` is the scorer/custodian side of the paired benchmark. It must not be mounted into the evaluated agent.

The module provides:

- canonical JSON and frozen-public-corpus validation;
- pristine fixture cloning into disjoint mutable namespaces;
- bounded trajectory normalization that strips non-observable fields;
- deterministic order, state, forbidden-action, outcome, and budget scoring;
- a versioned adapter (`evidence_adapter.py`, `beads.evidence-adapter.v1`) that re-derives typed events and outcome checks from schema-valid worker execution results, coordinator run checkpoints, and operation journals — the only sanctioned translation, rejecting caller-authored labels;
- bounded blinded semantic-report validation with no scoring authority;
- contaminated/unpaired/aliased pair rejection;
- exact macro/micro aggregation, Wilson bounds, telemetry tails/variance, exclusions, provenance, and paired lift;
- deterministic JSON/Markdown rendering and release-blocking exit codes;
- fail-closed threshold checking that recomputes the report from immutable pair receipts and honors manifest status and release-matrix blockers.

The public wrapper is `skills/beads/scripts/evaluate_skill.py`. All functionality is Python 3.12 standard library except validation of the already-frozen evaluation-result schema through the package's generated standard-library runtime.
