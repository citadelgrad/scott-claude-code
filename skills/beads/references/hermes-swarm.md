---
description: >-
  Parent protocol for bounded Hermes issue swarms, isolation, join accounting,
  distrusted results, immutable candidates, and per-issue completion.
metadata:
  tags: "beads, hermes, swarm, isolation, integration"
  source: "local Hermes-first architecture; verified against pinned v1 baselines"
---
# Hermes swarm

Use this route only for a finite set of independently writable, blocker-free
issues. `beads_coordinator.py` capabilities are the sole mechanical mutation
path for swarm and recovery state: claims, dispatch records, result imports,
lane freezes, candidate application, and tracker updates. The parent owns only
semantic choices, Hermes `delegate_task`/join/stop calls, and direct human
actions. It must not substitute native `bd`, ad-hoc Git mutation, or direct run
artifact writes for a coordinator capability. Workers are fungible
implementation children, not tracker owners or durable executors.

> **Enforcement boundary:** this reference specifies required behavior. It does
> not itself enforce concurrency, depth, filesystem scope, lifecycle ownership,
> hashes, or exactly-once effects. The later coordinator/runtime stages must
> validate and journal those properties. Plugin-free v1 cannot pre-block every
> child tool call or intercept a child summary before it reaches parent context.

The architectural rationale is summarized in [mental models](mental-models.md).

## Contents

- [Select and preflight](#select-and-preflight)
- [Claim, isolate, and dispatch](#claim-isolate-and-dispatch)
- [Join, stop, and classify](#join-stop-and-classify)
- [Verify and integrate](#verify-and-integrate)
- [Recompute or stop](#recompute-or-stop)
- [Explicit v1 limits](#explicit-v1-limits)

## Select and preflight

1. Invoke coordinator `start-run` with the caller-stable request identity. For
   an existing mapping, this means resume and reconcile that exact run rather
   than minting another. Do not ask for a front, claim, or dispatch until it has
   validated the request mapping, immutable manifest, repository/base identity,
   root ownership epoch, active `hermes.beads_run.v1` pointer, and exact pointer
   readback, and has accepted bootstrap checkpoint 2. Any absent, ambiguous, or
   conflicting readback stops the run before lane mutation.
2. Reconcile any active, partial, or inconsistent state through that capability
   before new mutation.
3. Resolve one finite non-writing coordination root and a bounded set of
   admissible lane issue IDs. Never dispatch the root as a writing lane.
4. Capture sanitized Beads state and fresh `bd ready` membership through the
   safe read transport. Reject cycles, missing acceptance criteria, blockers,
   unresolved gates, and IDs outside the frozen scope.
5. Derive intended write sets and calculate conflicts. Overlapping, aliased,
   case-ambiguous, symlink-ambiguous, broad-versus-narrow, or unknown scopes
   serialize; “the agents will resolve it” is not a conflict strategy.
6. Read live Hermes capability/configuration immediately before dispatch.
   Swarm mode requires all of:
   - top-level `delegate_task` is actually available to this parent;
   - `max_spawn_depth == 1`: the top-level parent creates depth-one children,
     and every child packet forbids delegation and sets `max_child_depth=0`;
   - `orchestrator_enabled` may be `false`: it controls nested child
     orchestration, not top-level flat delegation, and `false` is the safer v1
     setting;
   - manual packet-first worktree allocation and Hermes automatic child
     worktree isolation disabled, so the child cannot land in a path different
     from its frozen packet;
   - known positive configured child concurrency and known per-child
     iteration/timeout settings.
7. Re-read configuration at dispatch. Drift invalidates preflight.

The writing capacity is:

```text
min(3, configured Hermes maximum, remaining run budget, eligible lanes)
```

A repository may lower the cap. Do not infer the configured maximum from a
remembered default or exceed three. Normal ready-front computation may select a
legal front and identify lower-priority eligible IDs as deferred; those IDs
were never emitted. By contrast, reject an emitted `delegate_task` batch in
full if its cardinality exceeds the live configured limit—never truncate or
partially dispatch it. If top-level delegation/depth/isolation preflight fails,
swarm is unavailable; choose only an independently valid solo, read-only, or
durable-executor route.

## Claim, isolate, and dispatch

After `start-run` has accepted bootstrap checkpoint 2, execute this exact
coordinator/Hermes sequence for each legal front. A failed step does not permit
skipping to a later capability:

```text
coordinator front
coordinator claim-front
coordinator prepare-dispatch
parent Hermes delegate_task (one in-limit batch)
coordinator resolve-dispatch
parent Hermes join/stop and observe every child
coordinator record-result (once per returned lane)
coordinator freeze-lane (once per acceptable lane)
coordinator apply-candidate (only the exact verified candidate)
coordinator update-tracker (separate guarded note/close per integrated issue)
```

Within those capabilities, for each selected lane in deterministic order:

1. Refresh exact ready membership, tracker identity, assignee/status, blockers,
   gates, and cooperative ownership epoch.
2. Have `claim-front` perform the guarded claim saga: write intent, invoke
   native Beads, read back the exact issue, and classify the effect.
   Workers never claim, comment, update, close, add dependencies, or sync.
3. Allocate exactly one clean writing boundary:
   - default: a registered absolute Git worktree, unique branch, frozen base;
   - equivalent sandbox: separate writable filesystem, no shared mutable parent
     checkout, explicit base identity, and immutable export path.
4. Verify physical worktree, Git common directory, branch, base, HEAD, and clean
   initial state. A packet path or isolated conversation is not isolation proof.
5. Freeze one packet for one issue and one attempt. Its allowed paths do not
   overlap another active writing lane: every concurrent scope is explicitly
   non-overlapping. It names `.beads/**`, the primary
   checkout, parent run artifacts, and ungranted remote/external surfaces as
   forbidden.
6. Have `prepare-dispatch` record `DISPATCH_PREPARED` before the parent calls
   Hermes. Dispatch independent tasks in one in-limit batch. Because pinned
   Hermes supplies no per-task CWD, packet paths are absolute and every worker
   terminal call explicitly uses its worktree. Pass the observed dispatch
   outcome to `resolve-dispatch`; do not journal it ad hoc.

Children receive explicit context. They do not inherit parent reasoning,
conversation state, or loaded skills merely because Hermes may inject trusted
repository context files. They cannot ask the user; a needed decision returns a
blocked result. Each packet sets delegation forbidden and child depth zero.

## Join, stop, and classify

Account for every selected lane before verification or finish. A dispatch has
one of four outcomes:

- known child handles;
- known rejection proving no child ran;
- complete synchronous fallback, whose results enter the ordinary import path;
- otherwise `UNKNOWN`.

Never fabricate handles for synchronous fallback. Cardinality mismatch,
missing output, malformed identity, lost response, or insufficient process
observation is `UNKNOWN`, not failure or success.

A result joins only when run, issue, attempt, packet, ownership epoch, child
identity, schema/size/hash, imported outbox, and actual worktree state agree.
Record a complete batch accounting checkpoint before verifying any lane.

`stop` is cooperative. A stop request does not prove an in-flight command or
external effect stopped. Inspect process/task state, transcript evidence,
worktree/branch/HEAD, and any named external target. Finish only when every
child is joined, stopped and observed terminal, or durably recorded `UNKNOWN`.

Current user steering invalidates affected packet, result, verification,
review, and approval identities. Stop only affected handles; unrelated,
actually isolated lanes may continue. Quarantine cancelled, superseded,
stale-epoch, late, and duplicate-with-different-bytes results. None may
integrate, even if its content looks valid.

## Verify and integrate

`completed` means only that a worker claims its scoped implementation and local
checks completed. For every candidate lane the parent independently verifies:

1. exact run, issue, attempt, packet, base, physical worktree, branch, HEAD, and
   cooperative ownership epoch;
2. actual changed paths against both allowed scope and the claimed inventory;
3. the packet-selected immutable transfer identity:
   - scoped commit/tree only when current local-commit authority **and** an
     implemented packet-bound safe command path were present at dispatch;
   - otherwise a coordinator-created deterministic `patch_package`;
   - immutable `external_export` from an approved sandbox;
4. complete artifact inventory, byte hashes, and reproducibility;
5. fresh AC-relevant commands and required repository checks;
6. required independent lane review against the lane-freeze hash.

Changing transfer mode requires a new attempt and packet. Mutable dirty worker
state is never the integration target.

Verified lanes are not applied piecemeal to primary. Build a disposable
candidate from exactly:

```text
expected primary predecessor + ordered lane-freeze hashes
```

The resulting candidate tree/package hash is the integration target. For two
or more lanes, run integration checks and obtain a fresh non-implementer review
against that exact candidate hash. Preserve conflicts; never auto-resolve them.

Immediately before primary mutation, `apply-candidate` rechecks ownership
epochs, issue/gate eligibility, authority, clean primary predecessor, and
candidate hash. It applies and reads back the exact tested/reviewed candidate
only. If predecessor or candidate changed, return conflict and leave primary
unchanged. `update-tracker` performs separate guarded notes/closure per issue;
a successful sibling cannot close a failed, unknown, or unintegrated issue.

## Recompute or stop

After each accepted integration/closure, capture fresh Beads state, ready
membership, gates, and conflict matrix. Never reuse a static wave.

Stop dispatch on exhausted limits, two no-progress fronts, two materially
identical attempts for one issue, unavailable required verification, user
input, unsafe context pressure, or parent shutdown. Preserve worktrees,
immutable artifacts, journal/checkpoints, exact identities, and the next safe
action.

## Explicit v1 limits

- Three concurrent writing workers; no nested workers.
- A run manifest is at most 64 KiB; a child receipt is at most 2 KiB; a pending
  action or imported action receipt is at most 16 KiB; and parent synthesis is
  at most 4 KiB.
- One worker artifact is at most 10 MiB and all artifacts in one run total at
  most 100 MiB.
- Exceeding any byte/count limit fails closed. Before an effect, checkpoint and
  split only where the protocol defines a smaller independent continuation;
  otherwise refuse. Never silently truncate, summarize away required evidence,
  accept a partial batch, or replace an artifact with inline bulk content.
- Hermes `delegate_task` children are parent-bound, not restart-durable.
- Cooperative local ownership only; no fence against disconnected,
  cross-machine, or uncooperative writers.
- No cross-system transaction or exactly-once delivery.
- No automatic merge-conflict resolution.
- No automatic cross-actor Beads ownership transfer.
- No trusted persisted execution-approval adapter.
- No automatic production, spend, destructive, secret, Git-remote, or
  Dolt-remote effect.
- No Hermes plugin/hook, durable daemon, PAS reimplementation, generic tracker
  replacement, or direct Dolt-table access.
- Fixed run count limits also include 100 descendants, 10 ready fronts, two
  attempts per issue, and two no-progress fronts.
- A terminal, fully reconciled, ownership-validated run becomes cleanup-eligible
  only after 30 days. Active, blocked, failed-before-reconciliation, unknown,
  conflicted, ownership-bearing, or unintegrated runs have no age-based cleanup.
- Other harnesses are compatibility targets; Hermes behavior is normative.
