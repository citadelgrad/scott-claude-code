---
description: >-
  Bind each issue acceptance criterion to fresh target-specific evidence,
  independent review, guarded closure, and exact local or remote readback.
metadata:
  tags: "beads, verification, evidence, review, closure"
  source: "Beads 1.2.2 live help and frozen Hermes Beads v1 plan"
  verified: "2026-09-03"
---

# Verification and closure

Verified against Beads `1.2.2`. Native command success is one observation, not
proof that the issue's behavior, review, gates, or remote delivery succeeded.
Every model-visible Beads issue, gate, history, status, and mutation readback
must pass through a closed `safe_bd.py` profile; unavailable profile coverage is
`blocked` or `inconclusive`, never permission for native-output fallback.

## Contents

- AC-to-evidence ledger
- Evidence and failure classes
- Required checks and scope
- Review matrix
- Unavailable verification
- Closure guard and readback

## AC-to-evidence ledger

Create one row per stable acceptance ID:

| Field | Required content |
|---|---|
| AC ID | Exact stable criterion identifier |
| Observable | The behavior/state whose presence gives pass/fail |
| Check | Smallest authoritative command or inspection |
| Target | Repository/worktree, branch, `HEAD`/tree, artifact, and issue identity |
| Freshness | Start/finish time and proof target did not change afterward |
| Result | `passed`, `failed`, `unsupported`, `unavailable`, or `not_applicable` |
| Artifact | Sanitized evidence path and content hash when persisted |
| Gap | Exact behavior or environment not covered |

A zero exit code passes only when the check's assertions prove the named
observable. “Tests passed,” worker prose, schema validity, or a screenshot with
no bound target is too weak by itself.

## Evidence and failure classes

Use these evidence results exactly:

- `passed`: authoritative observable succeeded against the current target.
- `failed`: the observable was exercised and contradicted the criterion.
- `unsupported`: installed tool/version has no valid way to perform the check.
- `unavailable`: a required tool/service/environment could not be used now.
- `not_applicable`: criterion category truly does not apply, with reason.

For a failure, distinguish:

- implementation defect;
- introduced regression (target fails while valid base passes);
- pre-existing defect (valid base and target fail equivalently);
- invalid environment (the check did not exercise a valid target);
- unavailable verifier.

A pre-existing failure is not an introduced regression, but it remains a gap
unless issue/repository policy explicitly permits closure with that behavior.
Never relabel it to make the gate green.

## Required checks and scope

1. Re-read exact issue and current AC.
2. Freeze current target identity before checks.
3. Run the smallest criterion-specific checks with real assertions.
4. Run repository-required lint, type, build, integration, and full-suite gates
   applicable to changed paths.
5. Inspect `git status`, staged/unstaged diff, and untracked inventory. Compare
   actual paths to authorized scope and identify unrelated/pre-existing changes.
6. Verify persisted logs/artifacts are sanitized, current, bounded, and hashed.
7. Recheck target identity after evidence collection; target change makes old
   evidence stale.
8. Record every skipped, unsupported, unavailable, or narrowed check.

Do not substitute an easier unit test for a required integration test, a parser
check for semantic correctness, or an implementer's report for independent
inspection.

## Review matrix

A fresh reviewer distinct from the implementer must inspect a frozen target for:

- security, authentication, or permission boundaries;
- data, schema, or migrations;
- concurrency or atomicity;
- secrets or production infrastructure;
- cross-issue integration;
- any repository-classified high-risk change.

The review binds reviewer identity, immutable target hash, scope, exclusions,
findings, coverage, and verdict. If the target changes, the review is stale.

Only non-sensitive documentation or mechanical work may skip fresh review when
trusted policy permits it. Record the classification and disclosed omission;
“small diff” alone is not a risk classification.

## Unavailable verification

If a mandatory verifier is unsupported, missing, times out, lacks a valid
environment, or cannot bind the current target:

1. preserve all evidence that did pass;
2. record the exact required command/capability and error classification;
3. state the observable left uncovered;
4. choose `blocked` when a known prerequisite/action can resolve it, otherwise
   `inconclusive`;
5. name the next safe action;
6. do not close or render success.

A weaker check may replace it only when authoritative policy explicitly accepts
that substitution; disclose the narrowed coverage. Sunk cost, urgency, prior
passes, or reviewer confidence never turns unavailable coverage into pass.

## Closure guard and readback

Immediately before close, require affirmative evidence that:

- workspace, exact issue, ownership, branch, and target remain current;
- every AC ledger row required for this issue is `passed`;
- repository-required gates are fresh and passed;
- no active blocker, conflicting owner, unknown effect, or unrelated scope
  violation remains;
- every applicable gate has a typed affirmative target-bound result;
- required independent review passed against the unchanged frozen target;
- local tracker-close authority is active.

Then use guarded `bd close <exact-id> --reason <reason> --json`. Never teach or
use `bd update --status=closed`; never use `--force` to evade a failed guard.
Pinned native behavior can close an issue with no AC/evidence, proving native
success is necessary but insufficient.

After close, read the exact issue and required history through the safe
transport. Confirm ID, `closed`, close reason/causality, actor, and unchanged
target evidence. Classify uncertain effect rather than blindly closing again.

Finally inspect Git and Dolt state independently. Report local code completion,
local tracker closure, Dolt-remote synchronization, and Git-remote delivery as
four separate observed milestones. Remote success requires exact remote
readback; process exit alone is not enough.

Apply the named Team-maintainer versus Conservative/minimal and protected-branch
session-close rules in [Git and Dolt boundaries](git-and-dolt-boundaries.md).
For transition-specific recovery use [issue lifecycle](issue-lifecycle.md), and
for the complete one-issue sequence use [solo execution](solo-execution.md).
