---
description: >-
  Normative Beads dependency orientation, edge readback, and deterministic
  fresh-snapshot ready-front selection for tracked DAG work.
metadata:
  tags: "beads, dependencies, DAG, readiness, scheduling"
  source: "local normative contract grounded in Beads v1.2.2"
---

# Dependencies and ready fronts

Use this reference when creating or validating dependency edges, planning more
than one issue, or selecting a bounded set of writing lanes. It defines policy;
later coordinator stages provide mechanical enforcement. The parent/coordinator
is the only lifecycle writer in a swarm.

## Dependency orientation

A blocking edge is always:

```text
dependent -> prerequisite
```

Read `X -> Y` as **X needs Y**. Translate ambiguous prose before writing an
edge: “Y before X” becomes “X needs Y,” hence `X -> Y`. Do not infer order from
issue IDs, creation order, priority, or `parent-child` hierarchy.

Pinned Beads v1.2.2 exposes two equivalent forms:

```bash
bd dep add X Y --type blocks
bd dep Y --blocks X
```

The first argument to `bd dep add` is blocked by the second. In the shorthand,
the issue before `--blocks` is the prerequisite.

`parent-child` describes hierarchy, not ordinary prerequisite sequencing. A
child does not wait for its parent to close merely because it is a child.
Pinned v1.2.2 can nevertheless suppress descendants when their parent has an
active blocker or future defer state, so scoped readiness must still come from
live `bd ready` output.

### Exact edge readback

After every authorized edge add or removal, read both directions and the exact
dependent:

```bash
bd --readonly dep list X --direction down --type blocks --json
bd --readonly dep list Y --direction up   --type blocks --json
bd --readonly show X --json
```

For requested edge `X -> Y`, require all three facts:

1. the down-read at `X` contains `Y` with `dependency_type: "blocks"`;
2. the up-read at `Y` contains `X` with `dependency_type: "blocks"`;
3. `bd show X --json` contains `Y` in `X.dependencies` with that same type.

Compare the complete expected edge set, not a substring or command exit code.
A missing, extra, duplicate, reversed, or differently typed edge is
`CREATE_READBACK_FAILED`; preserve observed partial state and do not claim that
graph creation succeeded.

A structurally valid reverse edge is not self-identifying. Validate orientation
against an independent contract such as “X needs Y,” an approved DAG manifest,
or an expected fixture:

- observed reverse of the declared contract: `DEPENDENCY_ORIENTATION_MISMATCH`;
- no independent semantic contract: `DEPENDENCY_ORIENTATION_UNKNOWN`.

Both outcomes prevent dispatch. Never guess the intended direction from the
current ready set.

## Capture one fresh front

A ready front is immutable evidence for one captured instant, not a queue that
can be updated by inference. For root `R`, capture one bounded snapshot with:

- workspace identity, `bd` version, capture time, and canonical content hash;
- exact issue identity, type, parent, status, priority, assignee, acceptance
  criteria, defer state, labels, metadata, and coordination-only designation;
- complete relevant dependency and dependent records, including edge types;
- blocker, human-decision, and asynchronous-gate views;
- live capacity, worker budget, and conservative write-scope conflicts.

Separately capture authoritative ordered native membership:

```bash
bd --readonly ready \
  --parent R \
  --sort priority \
  --limit 0 \
  --json
bd --readonly blocked --parent R --json
bd --readonly dep cycles --json
```

`--limit 0` is required to avoid the default limit. Require unique IDs, complete
scope coverage, and agreement among issue, dependency, blocker, gate, and ready
views. `null` from `human list` or `gate list` may be normalized to an empty
collection only after successful exit and schema validation.

Do not use `bd ready --explain` as the scoped membership oracle on pinned
v1.2.2: its explanation path is diagnostic and does not honor normal ready
filters such as `--parent` consistently.

## Tracker readiness is necessary, not sufficient

| State | Meaning for dispatch |
|---|---|
| In fresh `bd ready` | Open and blocker-free under native semantics; necessary only |
| Selected | Chosen from this captured front; no tracker transition and no unlock |
| Claimed / in progress | Execution intent; native readiness normally excludes it |
| Attempt running or failed | Coordinator state only; resolves no dependency |
| Verified or reviewed | Evidence state only; resolves no dependency |
| Integrated | Code accepted into the target; resolves no dependency |
| Closed and read back | Accepted tracker transition; may unlock after recapture |
| Human/async gate open or unknown | Coordinator-ineligible regardless of another ready view |

A candidate is coordinator-eligible only when it is in the captured scope and
fresh ordered ready membership; is not `R`, an epic, gate, decision, or other
coordination-only issue; has required acceptance criteria; has consistent
identity and graph readbacks; has no conflicting owner or write scope; and
every required human or asynchronous gate has an affirmative target-bound
resolution.

Immediately before claim and again before dispatch, refresh exact status,
assignee, ownership, dependencies, blockers, gates, and ready membership. Native
claim does not atomically prove dependency readiness or provide cross-run
fencing. If a successfully claimed issue fails the refreshed guard, record
`CLAIMED_NOT_READY`, do not dispatch, and reconcile or compensate the claim.

### Human and asynchronous gates

Useful read views are:

```bash
bd --readonly human list --status=open --json
bd --readonly gate list --json
bd --readonly gate show GATE_ID --json
bd --readonly show GATE_ID --json
```

On v1.2.2, even `gate check --dry-run` is rejected under global `--readonly`.
Only the authorized parent may run the mutation-free probe without that flag:

```bash
bd gate check --dry-run --type=all --json
```

It may query external systems and is evidence only, never a resolving action.
Empty output, timeout, successful process exit, warning-only GitHub evaluation,
or unsupported local `bead` evaluation is not approval. After an authorized
resolution, repeat the exact gate views and recapture readiness.

## Deterministic bounded selection

First require the cycle readback to be a validated empty array. A nonempty
result refuses the whole front as `DEPENDENCY_CYCLE`; failed, malformed,
truncated, or unavailable cycle evidence is `CYCLE_CHECK_UNKNOWN`.

Use the exact order returned by the pinned authoritative `bd ready` command.
Do not invent a tie-breaker for live output. A synthetic fixture that has no
native ready output may use `(priority ascending, exact UTF-8 ID bytes)`, but
must label that fixture-only fallback.

Compute the hard live cap:

```text
hard_cap = min(
    3,
    live Hermes max_concurrent_children,
    remaining worker budget,
    any lower trusted repository cap
)
```

Malformed or zero capacity is `CAPACITY_UNAVAILABLE`. If the caller explicitly
requests more than `hard_cap`, return `CAPACITY_EXCEEDED`; never silently
truncate the request.

Then make one stable greedy pass over authoritative membership. Select the first
`hard_cap` coordinator-eligible candidates whose normalized write scopes do not
conflict with already selected lanes. Unknown overlap is a conflict. Identical
snapshot bytes, policy inputs, and capacity must produce byte-identical selected
IDs in the same order.

Selection, claim, execution, verification, review, and integration do not close
a prerequisite. Therefore a dependent cannot join the batch merely because its
prerequisite was selected or progressed in that batch. Return
`SAME_BATCH_UNLOCK_FORBIDDEN` for such an inference. Only an accepted close or
other contractually resolving lifecycle transition, exact readback, and a new
snapshot can introduce the dependent.

## Required refresh points

Discard the current front and capture a new one after every accepted change
that can affect eligibility or intended scheduling, including:

- dependency add, removal, or type change;
- issue create, close, reopen, claim, release, reassignment, or status change;
- defer, undefer, defer-time expiry, or priority change;
- gate resolution, rejection, timeout, escalation, or probe error;
- a new blocker, changed parent state, or changed coordination designation;
- worker failure, ownership change, or write-scope conflict change;
- interruption recovery, unknown effect, or inconsistent readback;
- immediately before every claim and every dispatch.

A lifecycle-affecting event after capture makes the old evidence
`STALE_READY_FRONT`. A stored hash identifies evidence; it never makes stale
evidence reusable.

## Conformance examples

Notation remains `X -> Y` = “X needs Y.” Expected arrays are exact selected
order after coordinator guards.

| Case | Fresh state or transition | Capacity | Expected | Rule |
|---|---|---:|---|---|
| Linear S0 | `A->B`, `B->C`, all open | 3 | `[C]` | Only leaf prerequisite ready |
| Linear S1 | Close/read back C; recapture | 3 | `[B]` | A remains blocked |
| Linear S2 | Close/read back B; recapture | 3 | `[A]` | A enters only the new front |
| Diamond S0 | `D->B`, `D->C`, `B->A`, `C->A` | 3 | `[A]` | Shared prerequisite only |
| Diamond S1 | Close A; recapture | 3 | `[B,C]` | Stable authoritative order |
| Diamond S2 | Close only B; recapture | 3 | `[C]` | D still needs C |
| Diamond S3 | Close C; recapture | 3 | `[D]` | Join becomes ready |
| Multi-root | B=P0, A=P1, C=P1; no edges | 3 | `[B,A,C]` | Fixture fallback ordering |
| Bounded | Same fixture | 2 | `[B,A]` | C stays ready, unselected |
| Repeat | Repeat identical bounded input 100 times | 2 | `[B,A]` | Byte-identical output |
| Same batch | `A->B`; B selected | 2 | `[B]` | Never infer `[B,A]` |
| New blocker | S0 has B; add `B->A`; recapture | 2 | `[]` for B | Cached membership discarded |
| Reprioritize | A=P1, B=P2; then B=P0 | 1 | S0 `[A]`, S1 `[B]` | Order is recaptured |
| Parent hierarchy | A/B only `parent-child` under P | 3 | `[A,B]` | No implicit sequencing |

The multi-root examples use the documented fixture fallback; live expected
order must be copied from authoritative `bd ready`, including its tie behavior.
Every changed live state must have a different canonical snapshot hash.

### Normative whole-front refusal fixtures

The following fixtures use normalized evidence notation, not a runtime wire
schema. Their shared state is exact: root `R` contains ordinary issues `A`, `B`,
and `U`; all three are open, have acceptance criteria, have no owner or write-
scope conflict, and are otherwise coordinator-eligible. `U` is independent and
appears in the stated native ready membership, so `[]` proves that each error
refuses the whole front rather than merely filtering the affected issue. The
hard cap is `2`; all unmentioned dependency, blocker, gate, identity, and issue
views are present, valid, empty where applicable, and mutually consistent.

#### Cycle refusal

Fixture state: blocking edges are `[A->B, B->A]`; validated cycle evidence is
`[[A,B,A]]`; ordered native ready membership is `[U]`; there are no required
gates. Expected selected IDs: `[]`. Expected typed reason:
`DEPENDENCY_CYCLE`.

#### Orientation refusal

Fixture state: the independent declared needs-contract is `[A->B]`, but the
complete observed blocking edge set is `[B->A]`; validated cycle evidence is
`[]`; ordered native ready membership is `[A,U]`; there are no required gates.
Expected selected IDs: `[]`. Expected typed reason:
`DEPENDENCY_ORIENTATION_MISMATCH`.

#### Human-gate refusal

Fixture state: there are no blocking edges; validated cycle evidence is `[]`;
ordered native ready membership is `[A,U]`; `A` requires human gate `H`, whose
target is exactly `A`, status is `open`, and affirmative resolution is absent.
Expected selected IDs: `[]`. Expected typed reason: `UNRESOLVED_HUMAN_GATE`.

#### Async-gate refusal

Fixture state: there are no blocking edges; validated cycle evidence is `[]`;
ordered native ready membership is `[A,U]`; `A` requires async CI gate `G`,
whose target is exactly `A`, status is `pending`, and affirmative result is
absent. Expected selected IDs: `[]`. Expected typed reason:
`UNRESOLVED_ASYNC_GATE`.

#### Unsupported-gate refusal

Fixture state: there are no blocking edges; validated cycle evidence is `[]`;
ordered native ready membership is `[A,U]`; `A` requires local `bead` gate `G`,
whose target is exactly `A`, and the pinned evaluator reports that gate type as
unsupported, with no affirmative result. Expected selected IDs: `[]`. Expected
typed reason: `GATE_UNSUPPORTED`.

#### Snapshot refusal

Fixture state: `show A` reports blocking dependency `[B]`, `dep list A
--direction down --type blocks` reports `[]`, and `dep list B --direction up
--type blocks` reports `[A]`; validated cycle evidence is `[]`; ordered native
ready membership is `[A,U]`; there are no required gates. Expected selected
IDs: `[]`. Expected typed reason: `SNAPSHOT_INCONSISTENT`.

## Typed refusals

| Reason | Condition and outcome |
|---|---|
| `CREATE_READBACK_FAILED` | Requested edge set differs; preserve partial state |
| `DEPENDENCY_ORIENTATION_MISMATCH` | Edge reverses declared needs-contract; no dispatch |
| `DEPENDENCY_ORIENTATION_UNKNOWN` | No semantic contract; inconclusive |
| `DEPENDENCY_CYCLE` | Validated cycle list is nonempty; refuse whole front |
| `CYCLE_CHECK_UNKNOWN` | Cycle evidence unavailable or invalid; inconclusive |
| `SNAPSHOT_INCONSISTENT` | Exact views disagree; refuse whole front and recapture |
| `STALE_READY_FRONT` | Eligibility-affecting event followed capture; discard it |
| `NOT_LIVE_READY` | Candidate absent from refreshed native membership |
| `SAME_BATCH_UNLOCK_FORBIDDEN` | Dependent relies on unclosed same-batch work |
| `UNRESOLVED_HUMAN_GATE` | No affirmative target-bound human result |
| `UNRESOLVED_ASYNC_GATE` | Timer, CI, PR, or external gate remains open |
| `GATE_UNSUPPORTED` | Required gate type/probe cannot be evaluated |
| `GATE_READBACK_FAILED` | Gate views error, conflict, or are ambiguous |
| `CAPACITY_UNAVAILABLE` | Live cap is zero, missing, or malformed |
| `CAPACITY_EXCEEDED` | Explicit requested batch exceeds hard cap |
| `CLAIMED_NOT_READY` | Post-claim refresh fails; reconcile, never dispatch |
| `CLI_CONTRACT_DRIFT` | Live help conflicts with relied-on command behavior |
| `UNSUPPORTED_BD_VERSION` | Installed Beads cannot satisfy the pinned contract |

Cycles, orientation mismatch, gate uncertainty, and inconsistent snapshots are
whole-front refusals: make no claim and dispatch no partial subset. Common
coordinator exits are `1` guarded refusal, `3` unavailable capability, `4`
conflict or stale identity, `5` unknown requiring recovery, and `6` human action
required. Runtime schemas introduced by later stages remain authoritative for
machine output; this document does not implement a scheduler or test fixture.
