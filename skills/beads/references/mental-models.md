---
description: >-
  Design models, consequences, and falsification tests for Beads lifecycle,
  Hermes swarms, isolation, verification, and recovery.
metadata:
  tags: "beads, mental-models, architecture, provenance"
  source: "local synthesis with cited primary pattern sources"
---
# Mental models

These models justify the protocol; they do not substitute for runtime guards.
Each useful model has an observable consequence and a falsification test.

The concrete enforcement boundary is singular: `beads_coordinator.py`
capabilities perform every mechanical swarm/recovery mutation. The parent owns
semantic decisions, top-level Hermes calls, and human actions only; workers and
durable executors receive no lifecycle mutation authority.

| Model | Protocol consequence | Falsification test |
|---|---|---|
| Finite-state machine | Lifecycle dimensions have guarded transitions; arbitrary status mutation is not completion. | Attempt premature close and resume from every nonterminal state. |
| [Single writer](https://mechanical-sympathy.blogspot.com/2011/09/single-writer-principle.html) | Coordinator capabilities alone write Beads/run lifecycle state; the parent supplies semantics and invokes Hermes/humans. | Attempt parent/child direct updates; reject both and accept only the guarded coordinator operation. |
| Actor/message model | Workers receive explicit frozen packets and return bounded records; no inherited reasoning. | Give a worker only its packet; it must execute or refuse correctly. |
| [Structured concurrency](https://rust-lang.github.io/async-book/part-reference/structured.html) | Every child rejoins, is observed stopped/terminal, or is durably `UNKNOWN` before finish. | Interrupt parent or one child; no unresolved result becomes accepted. |
| [Dynamic DAG scheduling](https://ocw.mit.edu/courses/6-042j-mathematics-for-computer-science-spring-2015/mit6_042js15_session17.pdf) | Dispatch fresh blocker-free ready fronts, never static phases or same-batch unlocks. | Exercise diamond, multiple roots, new blocker, partial failure, and cycle. |
| [Bulkheads](https://learn.microsoft.com/en-us/azure/architecture/patterns/bulkhead) | One issue, writable filesystem, branch, scope, outbox, and artifact boundary per lane. | Corrupt one lane; siblings stay inspectable and require their own evidence. |
| [Soft leases/fencing](https://research.google.com/archive/chubby-osdi06.pdf) | Assignee and elapsed time do not prove authority; bind monotonic ownership epoch and reject stale results. | Deliver worker A after reconciled reassignment to B; quarantine A. |
| [Idempotent requests](https://aws.amazon.com/builders-library/making-retries-safe-with-idempotent-APIs/) | Caller-stable semantic identity and immutable input precede each replayable effect. | Retry every boundary; no duplicate claim, note, merge, push, edge, or close. |
| Compare-and-swap / TOCTOU | Re-read readiness, ownership, gates, predecessor, and target immediately before mutation. | Change state between plan and action; stale action must refuse. |
| Event log | Append-only evidence plus immutable checkpoints reconstruct decisions without overwritten summaries. | Resume without chat and recover actor, activity, entity, outcome, and next action. |
| [Transactional outbox](https://microservices.io/patterns/data/transactional-outbox.html), constrained | Filesystem, Beads, Git, Hermes, and remotes cannot commit together; use a reconciliation journal, not an atomic outbox claim. | Crash on both sides of every intent/effect/readback boundary; expose every orphan. |
| [Provenance](https://www.w3.org/TR/prov-dm/) | Results bind entities (packet/artifact/target), activities (attempt/check), and actors (worker/reviewer/parent). | Swap target, actor, or digest; acceptance must fail. |
| [Circuit breaker](https://learn.microsoft.com/en-us/azure/architecture/patterns/circuit-breaker) | Bounded attempts/fronts stop repeated non-progress instead of consuming forever. | Two no-progress fronts or same-cause attempts must prevent another dispatch. |
| Information hiding | Parent consumes compact identities/receipts; raw issue sets, diffs, logs, and reasoning remain file-backed. | Large logs/diffs must not enter parent context or bypass byte caps. |
| Human-in-the-loop | Sovereign decisions are real boundaries, not decorative feedback. | Silence, timeout, confidence, stale approval, and rejection never authorize. |
| Agent fungibility | Task assignment supplies temporary role; no hidden specialist identity is required. | Rotate capable worker sessions/models across the same packet class. |

## Combined reading

The models reinforce one another:

- Single writer reduces lifecycle races, while fencing identities reveal that
  “one writer” is cooperative and local rather than a database-level lock.
- Structured concurrency accounts for child lifetime, while bulkheads contain
  filesystem failure and provenance binds every returned claim.
- Dynamic DAGs decide what may start; TOCTOU checks ensure that decision still
  holds at mutation time.
- Idempotency makes a known retry safe; the constrained outbox model preserves
  `UNKNOWN` when delivery cannot be known.
- Human gates retain sovereignty even when automation is otherwise resumable.

## Rejected or constrained models

- **Free-for-all stigmergy:** useful for discovery, unsafe as lifecycle
  ownership. Shared issue state is not permission for every worker to write it.
- **Static waterfall waves:** stale after blocker, failure, or priority changes;
  use live ready fronts.
- **Exactly-once delivery:** unavailable across Beads/Git/filesystem/Hermes;
  design for repeated attempts plus idempotent reconciliation.
- **Timeout as failure:** stopping is cooperative and side effects may continue;
  timeout produces unknown until observed.
- **Consensus for every decision:** expensive and unnecessary; one owner plus
  independent verification is sufficient for ordinary lanes.
- **Permanent specialist workers:** hidden identity reduces fungibility; use
  temporary implementer/reviewer separation instead.
- **Conversation as durable state:** unavailable after compaction/restart;
  reconstruct from Beads, Git/worktrees, and versioned artifacts.
- **Markdown as enforcement:** instructions cannot block tools, fence writers,
  authenticate approvals, validate hashes, or ensure exactly-once behavior.
- **Worktree path as isolation proof:** verify physical filesystem, Git identity,
  clean/base state, and no shared mutable checkout.
- **Schema validity as truth:** shape cannot prove scope, target, behavior, or
  test success; independent observation remains mandatory.
- **Native close success as gate success:** pinned Beads can warn and continue;
  require affirmative typed gate evidence first.
- **PAS as hidden owner:** durable execution does not transfer lifecycle
  authority; Hermes retains it in v1.

## V1 enforcement boundary

The reference prose may define roles, order, stop conditions, routing, evidence,
and recovery classifications. It must disclose limits and require later
coordinator capabilities. It does not mechanically:

- prevent child `bd`, filesystem, Git, network, or external calls;
- enforce worktree/scope/depth/capacity;
- fence uncooperative or cross-machine writers;
- coordinate multiple systems atomically;
- authenticate parent-attested approvals;
- validate schemas/hashes or guarantee retry cardinality.

Those become executable claims only after the owning later stages implement and
verify them. V1 deliberately ships no Hermes plugin/hook, nested workers,
automatic merge resolution, cross-actor ownership transfer, trusted reusable
execution approval, automatic protected/remote effects, durable daemon, PAS
replacement, generic tracker replacement, or direct Dolt-table access.

## Provenance discipline

Citations explain borrowed models; local consequences are design choices, not
claims that a source specifies this exact product. Accepted runtime evidence
must itself name immutable inputs and observed outputs. Do not use worker prose,
assistant confidence, or a merely valid JSON shape as provenance.
