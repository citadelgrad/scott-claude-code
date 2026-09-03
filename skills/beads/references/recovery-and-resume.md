---
description: >-
  Recovery order for interrupted Beads/Hermes runs, dangling effects, unknown
  children, stale claims, partial integration, and idempotent continuation.
metadata:
  tags: "beads, recovery, resume, journal, checkpoint"
  source: "local Hermes-first architecture; verified against pinned v1 baselines"
---
# Recovery and resume

Recovery precedes new mutation after compaction, restart, timeout, stale claim,
dangling operation, unknown child, partial candidate/application, or identity
mismatch. Conversation history is optional context, never durable truth.

Durable truth is the combination of **Beads state, Git/worktrees, and validated
versioned run artifacts**. A checkpoint alone is not sufficient.

> This is a decision protocol. `beads_coordinator.py` capabilities are the sole
> mechanical swarm/recovery mutation path and must enforce locks, ownership
> epochs, journal/checkpoint validation, probes, and idempotent effects. The
> parent owns semantics, Hermes calls, and direct human actions only. Prose must
> not be used as proof those guarantees ran.

## Contents

- [Recovery inputs and order](#recovery-inputs)
- [Probe outcomes and reconstruction](#probe-outcomes)
- [Unknown children and restart points](#unknown-children-and-late-results)
- [Damage and partial integration](#journal-and-checkpoint-damage)
- [Idempotency, retention, and reporting](#idempotency-rules)

## Recovery inputs

Locate a run only from:

1. explicit trusted run identity;
2. validated exact `hermes.beads_run.v1` root-issue metadata; or
3. the narrowly allowed owner-only unfinished-bootstrap ownership record when
   the pointer is absent and request/journal identity agrees.

Do not select “the newest directory.” Validate repository root, Git common
directory, Beads workspace, request mapping, run manifest, root/scope issue
identities, ownership history/current epoch, and artifact permissions before
using paths contained in the run.

## Ordered reconciliation

Recovery enters through coordinator `start-run` using the stable request
identity. It resumes an existing mapping when present. No `front`,
`claim-front`, or `prepare-dispatch` may run until `start-run` has validated the
manifest, root ownership epoch, active `hermes.beads_run.v1` pointer and exact
pointer readback, and accepted bootstrap checkpoint 2. Under the coordinator’s
run lock, recovery must then:

1. Validate the request-to-run mapping and immutable run manifest.
2. Validate the ownership chain/current record and retain expired ownership as
   `unknown`; expiry never authorizes theft.
3. Validate every journal record/hash and accepted checkpoint generation.
   The last valid `CHECKPOINT_ACCEPTED` event is authoritative. Rebuild a stale
   or missing `latest.json` only from that accepted extant generation.
4. Resolve the live Beads workspace and read sanitized exact issue, owner,
   dependency, blocker, readiness, gate, and run-pointer state through
   `safe_bd.py`; never query Dolt tables directly.
5. Enumerate registered worktrees, branches, primary/lane SHAs, clean/dirty
   state, immutable lane freezes, integration candidates, handoffs, and
   available child processes/transcripts.
6. Probe every dangling `PREPARED` effect against its exact target.
7. Reconstruct each issue’s completed work, blocker/gate, target identity,
   ownership epoch, accepted evidence, integration state, and next safe action.
8. Emit the reconciliation plan before mutation.
9. Perform only an unambiguous idempotent continuation; otherwise stop with
   conflict, unknown, or human action required.

## Probe outcomes

Every unresolved external effect maps observed state exactly:

| Observation | Resolution | Meaning |
|---|---|---|
| intended effect is present | `APPLIED` | Record exact readback; do not repeat. |
| declared pre-state is unchanged | `NOT_APPLIED` | Retry may be possible if all current guards still pass. |
| incompatible effect/state exists | `CONFLICT` | Preserve evidence; no replay. |
| observation cannot decide | `UNKNOWN` | Retain ownership/artifacts; no blind replay. |

A missing response never means not applied. A timeout never means clean
failure. An exit code never replaces exact readback.

## Per-issue reconstruction

Classify stale work as:

- `active`: matching live child/session, epoch, and observable lane progress;
- `recoverable`: no live child, matching artifacts, unambiguous next step;
- `completed-unclosed`: exact integrated target exists, tracker still open;
- `abandoned-clean`: no effect/artifact and proven no live owner;
- `conflicted`: another actor, epoch, target, or state advanced;
- `unknown`: evidence is insufficient.

Only recoverable and abandoned-clean states have routine continuation paths.
Completed-unclosed requires fresh verification of the exact integrated tree and
guarded close without reintegration. Conflict or unknown requires resolution,
not reassignment by age.

The reconstructed status must name, per issue:

- exact issue/run/attempt/epoch;
- tracker status, blockers, gates, and readiness;
- base, lane, frozen artifact, candidate, and primary target identities;
- worker/result/join state and whether any child remains unknown;
- accepted verification/review bound to the current hash;
- effects applied, not applied, conflicting, or unknown;
- one next safe action and authority needed.

This satisfies resume only when it can be derived from Beads plus artifacts
without chat history.

## Unknown children and late results

After parent interruption, every nonterminal `delegate_task` child begins as
unknown because children are parent-bound, not durable. Inspect known process,
transcript, worktree, outbox, and external targets. Do not redispatch while the
prior attempt could have run.

A dispatch response lost after `DISPATCH_PREPARED`, cardinality mismatch,
malformed receipt, missing result, stop during a tool call, or insufficient
process observation remains `UNKNOWN`. Claims and ownership remain held until
reconciled.

Duplicate byte-identical delivery may be recognized idempotently. A duplicate
with different bytes is conflict. Any result from a cancelled/superseded
attempt or older ownership epoch is quarantined, regardless of apparent
quality. A new attempt never legitimizes an old result.

## Restart points

Resume the existing operation instead of replaying the whole workflow:

| Last proven state | Next safe action |
|---|---|
| inert run, no ownership/effect | validate mapping; explicitly remove or continue bootstrap under its stable request |
| claim prepared | probe exact issue/history; resolve before code or dispatch |
| dispatch prepared/unknown | inspect Hermes/process/transcript/worktree; do not redispatch |
| worker returned | validate/import result and actual lane state |
| packaging prepared | probe package/inventory; reproduce or resolve unknown |
| lane packaged | rerun parent verification against lane-freeze hash |
| candidate built | verify/test/review that exact candidate; do not rebuild silently |
| primary integration prepared | compare predecessor and exact primary tree/ref |
| primary integrated, issue open | rerun mapped checks on exact integrated tree, then guarded note/close |
| protected/PAS launch unknown | probe receipt/target; never launch again blindly |

Every accepted transition creates a new immutable checkpoint generation; it does
not rewrite history to make the aggregate state look clean.

## Journal and checkpoint damage

Preserve original bytes before any narrowly specified final-tail repair. A
stale pointer may be reconstructed from the last accepted checkpoint. An
unaccepted stray generation is retained but not current.

A missing accepted generation, mismatched accepted digest, malformed/hash-bad
interior journal event, ambiguous final fragment, mismatched ownership history,
or changed request mapping is `CONFLICT` or `UNKNOWN` according to what can be
observed. Never truncate, reset epochs, delete the run, or fabricate a terminal
record to escape corruption.

## Partial integration

Partial candidate or primary integration stops new fronts. Preserve:

- expected predecessor;
- ordered lane-freeze hashes;
- candidate tree/package hash;
- actual applied and unapplied targets;
- conflicts and exact integration-test/review results;
- current primary tree/HEAD/status.

No affected issue closes until the actual integrated tree—not an intended
candidate—passes its mapped checks. Do not auto-resolve merge conflicts, apply
remaining lanes opportunistically, or call a partial push/sync shared success.

## Idempotency rules

Caller-stable semantic identity precedes every replayable effect. Repetition is
safe only when immutable input and ownership epoch are identical and exact
readback proves the already-applied intended state. Changed payload under the
same identity is conflict.

Claims, deterministic evidence notes, close, worktree creation, result import,
lane freeze, candidate application, push, and Dolt operations each need their
own target-specific probe. The reconciliation journal exposes dual-write
uncertainty; it is **not** a transactional outbox and cannot promise atomicity
or exactly-once delivery.

## Retention and handoff

Keep active, blocked, failed-before-reconciliation, unknown, conflicted,
unintegrated, and ownership-bearing runs regardless of age. Only terminal,
fully reconciled, ownership-validated runs become cleanup-eligible after 30
days. Never use a broad recursive cleanup or delete an unverified path. This is
an eligibility floor, not automatic deletion.

If continuation must survive another parent restart, a long unattended run,
resumable human wait, or fixed stage graph, prepare a sanitized hash-bound
durable handoff before launch. Hermes retains lifecycle ownership; the external
executor is Beads-read-only and returns an immutable result for ordinary parent
verification/integration.

## Recovery report

Report exact recovered identities and observed state, commands/probes run,
mutations (if any), unresolved evidence, preserved artifacts, and next safe
action. Valid non-success statuses include `blocked`, `inconclusive`,
`conflict`, `UNKNOWN`, and `HUMAN_ACTION_REQUIRED`; never translate them into
“done.”
