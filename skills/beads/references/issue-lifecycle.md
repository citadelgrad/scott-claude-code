---
description: >-
  Apply guarded Beads claim, pause, block, defer, close, reopen, reassignment,
  and uncertain-effect lifecycle transitions.
metadata:
  tags: "beads, lifecycle, claim, close, reopen, recovery"
  source: "Beads 1.2.2 live help and frozen Hermes Beads v1 plan"
  verified: "2026-09-03"
---

# Issue lifecycle

Verified against Beads `1.2.2`. Consult live help for current status values and
flags. Never teach a transition from remembered syntax when help has drifted.
All model-visible help, issue, readiness, blocker, gate, history, and mutation
readback uses a closed `safe_bd.py` profile. Missing profile coverage blocks the
operation; native `bd` output is never a model-facing fallback.

## Contents

- State model
- Claim contracts
- Transition guards
- Pause, block, defer, and reassign
- Close and reopen
- Uncertain-effect recovery

## State model

Keep two dimensions separate:

1. **Persisted lifecycle status:** the exact status returned by `bd show`.
2. **Effective eligibility:** live readiness, dependency blockers, deferral,
   gates, ownership, and policy.

“Blocked” is often a derived condition. `bd blocked --json` can return an issue
whose stored status is `open` or `in_progress`; do not invent a persisted
`blocked` status. Only `bd ready` blocker-aware semantics prove current ready
membership. Refresh both dimensions before every guarded transition.

## Claim contracts

### Queue selection

`bd ready --claim --json` chooses and claims in one native transaction, but its
target is unknown before mutation. Use it only in a coordinator operation with
a caller-stable write-ahead identity and exact post-interruption selection/effect
reconciliation. The direct route must use a sanitized read-only `ready` profile,
preserve one exact ID, refresh its eligibility, and use the guarded known-issue
claim below. This introduces a harmless selection race: a losing claim is a
conflict and selects nothing else. It avoids an unrecoverable crash window in
which replay of unknown-target queue-pop could claim a second issue.

### Known issue

`bd update <exact-id> --claim --json` atomically sets assignee to the actor and
status to `in_progress`; it is idempotent when already claimed by that actor.
Use it only after a fresh exact-ID readiness/status/assignee/blocker check.
Native known-ID claim checks claimability, not dependency readiness: pinned
`1.2.2` can claim an issue that `bd blocked` still reports.

After either operation, require exact ID, expected actor, `in_progress`, current
eligibility, and resolved effect from exact readback. Same actor is not a
particular Hermes-run fence. Changed state, another owner, stale cooperative
identity, or unknown effect prevents implementation.

If known-ID postclaim checks fail, retain that operation/ID and reconcile first.
Release or reassignment is compensation and requires its own authority and
readback; never claim another candidate while the first effect is unresolved.

## Transition guards

| Transition | Required guard | Required readback |
|---|---|---|
| `open` → `in_progress` | healthy intended workspace; testable AC; named issue currently ready or queue-pop selected atomically; no owner conflict; active local authority | exact ID/status/actor plus current blockers/gates/readiness |
| `in_progress` → `closed` | complete fresh AC evidence; required checks/review; no blockers; affirmative gates; current target and close authority | exact ID, closed state, reason/causality, history as needed |
| any mutation | expected pre-state, exact target, active authority, stable operation identity | exact target after-state and effect classification |
| stale/partial/unknown → any | reconcile existing effect first | `NOT_APPLIED`, `APPLIED_AND_READ_BACK`, `CONFLICT`, or `UNKNOWN` |

The guarded route detects time-of-check/time-of-use changes through immediate
precheck and post-effect readback. It does not claim that Beads provides a
native compare-and-swap revision or target-side run fencing.

## Pause, block, defer, and reassign

- **Pause:** leave a concise durable note with target identity, completed
  evidence, remaining work, blocker, and next safe action. Do not infer
  abandonment from a clean worktree.
- **Block:** record the actual dependency/gate condition with current supported
  syntax. Preserve stored status exactly; report the derived blocked condition.
- **Defer:** inspect current help and use the dedicated supported defer behavior,
  then verify timestamp and absence/presence in ready output as intended.
- **Reassign/release:** never steal silently. Inspect actor, history, worktrees,
  active processes, and cooperative ownership. Require explicit authority and a
  resolved release/reassignment readback.
- **Supersede/cancel:** preserve lineage and reason with the live dedicated
  relationship/status semantics. Do not overwrite evidence merely to make the
  queue clean.

## Close and reopen

Use `bd close`, not `bd update --status=closed`. The dedicated close path owns
open-child, blocker, gate, pinned-item, reason, and hook behavior. Do not use
`--force` during normal execution. Native refusal to close an issue with an
open blocker is a real guard; native acceptance is still not proof that AC,
verification, review, or gate policy passed.

Use `bd reopen <exact-id> --reason <reason> --json`, not a generic status
update. In `1.2.2`, reopen sets status to `open`, clears closure metadata, and
emits the dedicated Reopened event. Verify all of those effects before claiming
repair.

## Uncertain-effect recovery

Classify every attempted lifecycle effect:

- `NOT_APPLIED`: exact probe proves the pre-state remains; retry only with the
  same intent and authority.
- `APPLIED_AND_READ_BACK`: exact intended state and causality are observed;
  continue without replay.
- `CONFLICT`: another incompatible effect/state is observed; stop and reconcile
  ownership/intent.
- `UNKNOWN`: observation cannot establish either state; preserve evidence and
  do not replay blindly.

Urgency, same-assignee idempotence, a clean checkout, or a zero exit code never
turns `CONFLICT`/`UNKNOWN` into ownership or completion.

For the end-to-end route see [solo execution](solo-execution.md); bind closure
to [verification and closure](verification-and-closure.md).
