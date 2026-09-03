---
description: >-
  Decision boundary between interactive Hermes delegation and durable PAS or
  external execution, including retained-ownership handoff semantics.
metadata:
  tags: "beads, hermes, pas, durable-handoff"
  source: "local Hermes-first architecture and pinned PAS comparison"
---
# Hermes or PAS/durable execution

Hermes delegation and PAS are alternative execution modes over shared Beads
project truth. Do not force PAS ceremony onto routine interactive work, and do
not pretend parent-bound `delegate_task` children survive a restart.

`beads_coordinator.py` capabilities remain the sole mechanical swarm/recovery
mutation path in either mode. The parent owns the semantic route choice, Hermes
calls, and direct authorized human/harness launch only; PAS never gains Beads
lifecycle authority.

## Contents

- [Architecture comparison](#architecture-comparison)
- [Selection and redirect](#choose-hermes)
- [Retained-ownership handoff](#retained-ownership-handoff)
- [Unknown launch and return](#unknown-launch-and-return)
- [PAS limitations and non-goals](#pas-limitations-to-preserve)

## Architecture comparison

| Dimension | Hermes issue swarm | PAS/durable executor |
|---|---|---|
| Plan | Dynamic ready fronts and user steering | Fixed auditable stage/resume topology |
| Parallelism | Real bounded issue-level fan-out/fan-in | Depends on pipeline; pinned `attractor-6im.dot` is serial single-lane |
| Lifetime | Children bounded by parent process/session | Durable checkpoints/log state across interruption |
| Context | Parent retains current conversation; packets make children explicit | Fresh processes rehydrate from durable artifacts |
| Isolation | One issue/worktree or equivalent per lane | Executor-specific workdir/sandbox plus immutable export |
| Review | Selective lane review; mandatory exact combined-candidate review for multi-lane integration | Fresh review/quality nodes can be mandatory graph stages |
| Gates | Dynamic typed gates and direct human interaction | Durable wait nodes and predefined transitions |
| Adaptability | High during a run | Lower; graph encodes expected branches |
| Cost/budget | Selective child spend; live Hermes limits | Repeated startup/rehydration; pinned Codex pipeline reports `$0`, so step/time limits matter more than USD budget |
| Best fit | Short interactive, ambiguous, dynamically selected independent issues | Restart survival, unattended duration, resumable waits, fixed separation |

## Choose Hermes

Use Hermes solo or bounded delegation when:

- work fits the active parent session;
- issue selection depends on live Beads readiness;
- the user may steer scope/priorities interactively;
- real issue-level parallelism is useful;
- at most `min(3, configured Hermes maximum)` isolated writers suffice;
- every child can be joined/stopped or durably classified unknown before finish.

## Redirect before dispatch

Use PAS or another approved durable executor before child dispatch when any of
these is required:

- survival across parent restart;
- hours/days of unattended execution;
- resumable human waits;
- a fixed, auditable stage graph or mandatory process separation;
- lifetime beyond Hermes child timeout/process semantics.

If lifetime requirements change mid-run, reconcile all children and frozen
state first. Do not launch a durable executor while a possibly active child
owns the same mutable lane.

## Retained-ownership handoff

The later coordinator’s `prepare-handoff` capability freezes a sanitized,
hash-bound handoff containing:

- exact run, root and scoped issue IDs, and currently held ownership epochs;
- tracker/dependency/ready-front/authority snapshot hashes;
- base commit and approved isolation/export target;
- unresolved gates and protected-action boundaries;
- executor type and exact identity;
- fixed topology, resume points, budgets, and result contract;
- allowed/forbidden code, Git, remote, and external effects;
- explicit read-only Beads authority.

It emits a pending launch action. It does **not** launch PAS, transfer lifecycle
ownership, or authorize protected effects. The parent/current harness performs
the external launch only with current authority and returns a bound receipt.

In plugin-free v1:

1. Hermes retains every lifecycle ownership epoch.
2. PAS/external executor receives no Beads mutation authority.
3. The executor returns a bound immutable commit/export plus evidence.
4. A resumed/new authorized Hermes parent passes the returned receipt/artifact
   to coordinator `record-durable-result`.
5. `record-durable-result` journals and imports the immutable executor result
   only after validating handoff, executor, run/issue/epoch, artifact, target,
   limits, and the no-Beads-mutation declaration. No verification or integration
   begins until this capability accepts the result.
6. Valid work enters the ordinary parent scope/AC/review/candidate/readback path.
7. Coordinator `update-tracker` alone performs guarded tracker notes/closure.

There is no automatic cross-actor claim transfer. Beads 1.2.2 lacks a
conditional atomic transfer primitive; calling this an “ownership transfer”
would hide an unsafe saga.

## Unknown launch and return

Record launch intent before acting. A missing or ambiguous launch receipt is
`UNKNOWN`; Hermes retains claims and must probe process/executor/export state.
Never launch again blindly.

Reject wrong executor identity, handoff digest, ownership epoch, issue scope,
base/export target, expired handoff, mutable/non-reproducible artifact, or any
executor Beads mutation. Duplicate identical results may be idempotent; late or
stale results are quarantined.

A valid immutable export still does not close an issue. Parent verification,
required review, exact integration candidate, primary readback, and separate
tracker close remain mandatory.

## PAS limitations to preserve

The pinned 18-node/29-edge `attractor-6im.dot` is a checkpointed serial state
machine using fresh ephemeral Codex processes against one shared worktree; it
is not true issue-level fork/join. PAS offers durable node boundaries and human
waits, but:

- a node can mutate externally and die before checkpoint advance, leaving an
  uncertain effect that still needs target-specific reconciliation;
- checkpoint identity is path-based rather than bound to DOT content;
- `$0` Codex accounting does not make work costless or USD budget meaningful;
- shared-worktree execution is not equivalent to parallel isolated lanes.

Do not reproduce `pas run` instructions here or reimplement PAS in the Beads
skill. This reference only selects the executor and defines the safe handoff.

## Explicit non-goals

V1 provides no daemon/scheduler, hidden lifecycle transfer, generic tracker API,
trusted persisted execution approval, automatic remote/protected action, or
pre-model child-summary interception. The handoff protocol is guidance until
the later runtime capabilities and schemas validate it.
