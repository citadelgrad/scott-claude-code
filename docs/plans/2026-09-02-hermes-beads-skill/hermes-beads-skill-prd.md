# Hermes Beads Skill — Product Requirements Document

**Status:** Draft for review
**Date:** 2026-09-02
**Owner:** Scott Nixon ([@citadelgrad](https://github.com/citadelgrad))
**Planning issue:** `scc-a3d`
**Pairs with:** [hermes-beads-skill-spec.md](./hermes-beads-skill-spec.md)
**Product form:** Hermes-first Agent Skill, locally maintained; portable where the Agent Skills standard permits

> This document defines why the skill exists and what behavior it must produce.
> The paired SPEC defines exact files, contracts, algorithms, fixtures, and build order.
> Implementation must not begin until both documents survive the planning review loop and Scott approves decomposition.

---

## 1. Executive decision

Build a Tier-2 `beads` skill package whose primary product is a compact decision/procedure skill backed by one explicit coordinator runtime for swarm and recovery operations. It is not a replacement issue tracker and it must not hide runtime-grade guarantees in Markdown.

The skill will make Hermes reliably do six things:

1. discover the correct Beads workspace and load current CLI guidance;
2. bind meaningful repository work to an explicit issue before mutation;
3. treat the Beads dependency DAG as a dynamic ready front rather than a static phase list;
4. maintain durable issue state that survives compaction, interruption, and handoff;
5. coordinate bounded subagent swarms without losing ownership, isolation, or evidence;
6. refuse to close work until current acceptance criteria and fresh verification evidence agree.

Version 1 is a skill package with references, schemas, an explicit `beads_coordinator.py` runtime, fixtures, and live-agent evaluations. Solo/read/planning paths preserve native `bd` semantics through the narrow in-memory safe-output adapter rather than exposing raw issue payloads. The runtime owns write-ahead journal/checkpoint transitions and guarded native `bd`/Git mutations for swarm/recovery; it emits/accepts bounded records around Hermes-only actions such as `delegate_task`. It is not an OSS proposal and does not require a Hermes plugin. A narrowly scoped hook companion remains a later option only if paired evaluation proves that skill/runtime use still leaves material violations.

The default operating model is:

- **one coordinator authority**—the parent Hermes session plus bundled runtime—owns Beads lifecycle mutations;
- **zero or more isolated workers** own code lanes, persist bounded evidence manifests, and return small receipts;
- **Beads** owns durable project truth;
- **Git worktrees** own code isolation;
- **repository-native tests and linters** own implementation truth;
- **the user** owns irreversible or explicitly sovereign decisions.

---

## 2. Product vision

Beads should feel native to the way Hermes works rather than like a checklist bolted onto the side.

A user should be able to ask:

- “Fix this bug.”
- “Build `scc-123`.”
- “Take the next ready issue.”
- “Run the independent children of this epic in parallel.”
- “Resume what we were doing before compaction.”
- “What needs me?”

Hermes should then choose the correct Beads lifecycle without being micromanaged. It should know when to use one issue, when to create a dependency graph, when to stay read-only, when to claim, when to checkpoint, when to delegate, when to stop, and when closure would be dishonest.

The product is successful when Beads becomes the quiet control plane for project work:

- no orphaned implementation without a durable issue;
- no duplicate workers on the same issue;
- no “done” state based only on an agent summary;
- no dependency inversion caused by phase-language confusion;
- no context loss after compaction;
- no parallel swarm writing through one dirty checkout;
- no routine tracker ceremony for trivial, ephemeral actions.

---

## 3. Problem statement

### 3.1 Current strengths

Scott’s current Beads use already has strong issue quality, detailed acceptance criteria, dependency graphs, and evidence-rich close reasons. The existing repository also contains valuable pieces:

- the official Beads skill is installed as a compatibility copy under `.agents/skills/beads/`;
- `plugins/beads-epic-builder/commands/epic-swarm.md` describes wave planning, worktree isolation, checkpoints, worker manifests, and independent spot checks;
- `skills/delegate-first/SKILL.md` requires isolated worktrees and independent verification of worker self-reports;
- `scripts/verify_orchestration_contracts.py` defines reusable scope, fan-out, artifact, failure, continuation, and mechanical-test contracts;
- `skills/adversarial-reviewer/` demonstrates schemas, deterministic validators, paired controls, frozen benchmarks, and provenance;
- `scripts/verify_skills_distribution.py` mechanizes portable-skill inventory, metadata, grouping, authorship, and drift checks.

### 3.2 Current gaps

The useful behavior is fragmented across injected `bd prime` text, an upstream compatibility skill, repository instructions, a Claude-specific epic-builder plugin, and unrelated orchestration skills.

That fragmentation causes predictable failures:

1. **Trigger failure:** the Beads skill may not be selected when a user asks for ordinary repository work without saying “Beads.”
2. **Compliance failure:** an agent can load a skill yet skip claim, checkpoint, dependency, or closure steps.
3. **Boundary failure:** an agent may use Beads for trivial scratch work or mutate project state when it should remain read-only.
4. **Ownership ambiguity:** parent and child agents can both believe they own tracker mutations.
5. **Isolation ambiguity:** workers can share a checkout or overlapping write sets.
6. **Self-report trust:** worker claims can be accepted without inspecting the actual worktree and rerunning evidence.
7. **Static-wave drift:** a precomputed wave can become stale when dependencies, blockers, or failures change.
8. **Context bloat:** raw issue JSON, test logs, diffs, and worker output can consume the coordinator context.
9. **Version drift:** copied CLI documentation becomes false as `bd` evolves.
10. **Distribution drift:** the skill can exist locally but be omitted from portable manifests, authorship inventory, docs, or verification registries.

### 3.3 Why the upstream skill is not enough

The upstream skill is a strong reference and should remain credited. It covers the core session protocol, persistent memory, dependencies, worktrees, gates, and resumability. It intentionally serves multiple harnesses and general users.

Scott’s local skill needs additional opinionated behavior:

- Hermes-specific `delegate_task` semantics;
- parent-owned Beads mutation for bounded child swarms;
- independent verification of child self-reports;
- bounded context and artifact contracts;
- repository quality gates and team-maintainer/conservative authority rules;
- live paired skill evaluation;
- explicit comparison with PAS CLI orchestration;
- compatibility with this repository’s portable-skill distribution and authorship rules.

The local skill should adapt and extend upstream material, not erase attribution or fork the `bd` command reference into stale prose.

---

## 4. Users and jobs to be done

### 4.1 Primary user

Scott, working interactively in Hermes across repositories that use `bd` for granular engineering work.

Primary jobs:

- start a tracked coding task without repeating Beads ceremony;
- see whether work is ready, blocked, stale, or waiting on a human;
- preserve enough state to resume after compaction or days away;
- exploit parallel agents when issues are genuinely independent;
- keep tracker state truthful while agents fail, retry, or partially succeed;
- finish with verified evidence and the right commit/push/sync policy.

### 4.2 Secondary users

- future agents resuming Scott’s work with no conversation history;
- Codex or Claude Code consuming the portable subset of the same skill;
- maintainers installing selected `scott-cc` skills with `npx skills add`;
- reviewers evaluating whether a closed Bead was genuinely satisfied.

### 4.3 Non-users

- generic project managers seeking a GUI;
- teams wanting a hosted tracker replacement;
- agents operating in repositories without Beads unless the user explicitly asks to initialize it;
- PAS CLI itself; PAS remains a separate orchestrator that can consume Beads state.

---

## 5. Product principles

### P1. Beads is project truth; the skill is policy

The skill never invents a parallel tracker. Beads issues, dependencies, notes, labels, decisions, and status remain authoritative. Any run checkpoint exists only to resume orchestration and must reconcile back to Beads.

### P2. Live CLI behavior beats copied documentation

Every run starts from `bd prime`, `bd where`, and command-specific `--help` when needed, with model-visible output routed through closed safe-output profiles. Static references explain concepts and local policy, not every flag.

### P3. Claim is a lease on intent, not proof of progress

A claim means one execution owner may act. It is soft ownership, not a fenced lease: it does not prove the actor is alive, prevent a paused worker from editing code, prove code changed, or prove tests passed. Stale claims require explicit inspection and recovery; a late result from an old run must be rejected when its run/attempt identity no longer matches the current owner.

### P4. One lifecycle writer per bounded swarm

For Hermes `delegate_task` swarms, the parent coordinator owns issue claims, status, notes, follow-ups, and closure. Workers run `bd` read-only if they need issue data and return manifests. A unique local ownership epoch/token prevents two cooperating same-actor Hermes runs from both treating an idempotent Beads claim as their own. This is cooperative local fencing, not a claim that Beads v1.2.2 prevents uncooperative or disconnected writers.

### P5. Parallelism comes from the ready front

The dependency DAG defines what can run now. The coordinator recalculates readiness after completions and failures. It does not freeze the entire epic into irreversible “phases.”

### P6. Worktrees isolate code; issue IDs isolate responsibility

Each writing worker receives one issue, one isolation boundary, one allowed write set, and one integration identity. A Git worktree/branch is the default. A container or sandbox is acceptable only when it provides a separate writable filesystem, immutable export artifact, explicit base identity, and no shared mutable checkout. Parallel work without a verified equivalent boundary is rejected.

### P7. Reports are claims until independently checked

A worker’s summary, test status, or “done” statement is not evidence. The parent reads the actual diff and artifacts and runs the relevant verification before changing Beads to closed.

### P8. Closure is a state transition with guards

An issue may close only when acceptance criteria are mapped to current evidence, blockers are resolved, required review is complete, and authority policy permits the associated Git/sync actions.

### P9. Fail closed on ambiguity that risks corrupting state

Wrong workspace, ambiguous issue identity, conflicting ownership, dependency cycles, overlapping write sets, stale target hashes, malformed manifests, and inconclusive verification stop mutation. Missing optional convenience features degrade honestly.

### P10. Progressive disclosure is part of correctness

The main `SKILL.md` stays small enough to be followed. Detailed procedures load just in time from focused references. A comprehensive monolith that models skip is not “more rigorous.”

---

## 6. Scope

### 6.1 Version 1 scope

Version 1 includes:

- a portable `skills/beads/` Tier-2 skill package;
- Hermes-first coordinator instructions;
- solo, resume, issue-creation, ready-front, parent-coordinated swarm, human-gate, and session-close workflows;
- bounded worker and checkpoint schemas;
- deterministic validators and fixture tests;
- a frozen paired benchmark with no-skill and with-skill conditions;
- trigger, compliance, boundary, outcome, recovery, cost, latency, and variance metrics;
- integration into `skills.sh.json`, repository docs, authorship inventory, orchestration verification, and catalog counts;
- migration guidance from the installed upstream compatibility skill and the Claude-only `beads-epic-builder` behavior;
- explicit documentation of what remains PAS CLI territory.

### 6.2 Deferred scope

- a Hermes plugin with `on_skill_lifecycle`, `pre_tool_call`, or `pre_verify` callbacks;
- a new CLI that wraps `bd`;
- modifying upstream Beads;
- an OSS proposal or upstream pull request;
- automatic global installation into other Hermes profiles;
- a daemon or durable worker scheduler;
- replacing PAS CLI, Reckoner, Foundry, Hermes Kanban, or Agent Mail;
- fully autonomous production mutation;
- cross-machine distributed locking beyond Beads’ own contracts.

### 6.3 Explicit non-goals

1. **Do not duplicate `bd --help` in Markdown.** It will rot.
2. **Do not make every task a Bead.** Trivial current-turn mechanics remain local execution details.
3. **Do not allow a skill to imply enforcement it cannot provide.** Markdown guidance is not an interceptor.
4. **Do not make children secretly inherit parent state.** Every isolated child receives explicit context.
5. **Do not create specialist worker personas for ordinary implementation.** Workers remain fungible generalists; separation of duties is expressed by assignment, not permanent identity.
6. **Do not close an entire epic because some children succeeded.** Completion is per issue and evidence-backed.
7. **Do not auto-commit, push, or sync without the active repository/user policy authorizing it.**
8. **Do not place secret values in issues, notes, checkpoints, manifests, logs, or fixtures.**
9. **Do not rely on the user to notice silent degraded coverage.** Degradation must be disclosed.
10. **Do not force PAS-style ceremony onto interactive one-issue work.**

---

## 7. Entry intents, overlays, and transitions

Routing is compositional, not seven mutually exclusive labels.

- **Primary entry intent (exactly one):** observe, execute-one, execute-set, or plan/create.
- **Orthogonal overlays (zero or more):** recovery-required, human/async-gated, durable-executor-required.
- **Lifecycle transitions:** an execution may become gated or recovery-required; planning may transition to execution only after explicit authorization; an execute-set may redirect to durable execution when lifetime requirements change.

Precedence is deterministic: recover inconsistent/unknown existing state before new mutation; satisfy unresolved gates before protected effects; redirect to a durable executor before spawning parent-bound children when work must outlive the session; otherwise execute the selected primary intent. Evaluation scores the allowed observed route/transition path, not whether the agent printed one expected mode label.

### 7.1 Primary intent — Observe/read-only

Use when the user asks what is ready, blocked, stale, assigned, or waiting on a human.

Behavior:

- establish workspace with the safe `bd where` profile;
- run read-only native JSON queries internally through the safe adapter where parsing is needed;
- do not claim, update, create, close, sync, commit, or push;
- report exact issue IDs and state.

### 7.2 Primary intent — Execute one issue

Use for one coherent issue that one Hermes session can implement.

Behavior:

- run preflight;
- inspect exact issue and acceptance criteria;
- claim through the guarded native operation/readback protocol and stop if ownership or readiness changed;
- establish target baseline;
- implement and checkpoint material decisions;
- run acceptance-mapped verification;
- update/close honestly;
- perform Git and Dolt actions only under active authority policy.

### 7.3 Primary intent — Execute an issue set with a Hermes swarm

Use when two or more ready issues are independent enough to run concurrently and each writing lane can be isolated.

Behavior:

- parent remains coordinator and sole lifecycle writer;
- parent computes a current ready front and conflict matrix;
- parent claims selected issues before dispatch;
- each worker receives one self-contained packet and one verified isolation boundary (Git worktree by default);
- workers use the packet-bound `safe_bd.py` transport, whose read profiles apply internal `bd --readonly`; direct `bd` is forbidden and workers never change lifecycle state;
- children return bounded, schema-valid evidence manifests;
- parent independently verifies each lane;
- parent integrates and closes per issue, recalculating the ready front after each accepted result.

### 7.4 Overlay — Durable executor required

This is not implemented by Hermes `delegate_task` because those children are bound to the parent session. When work must survive parent interruption or run for hours/days, the skill redirects to a durable executor such as PAS CLI, separate Hermes processes/worktrees, or an approved scheduler.

Redirect when any of these are required: survival across Hermes restart, long unattended duration, resumable human waits, or fixed auditable stage topology. Before transfer, Hermes writes a sanitized, hash-bound handoff containing root issue, ready candidates, dependency snapshot, authority boundaries, base commit, required gates, executor identity, and cooperative ownership epoch. In v1 Hermes **retains lifecycle ownership**: the durable executor gets read-only Beads access, produces durable results, and a resumed/new authorized Hermes coordinator reconciles, verifies, integrates, and updates/closes. Native Beads v1.2.2 cannot atomically transfer an existing claim between actors, so independent lifecycle ownership transfer is explicitly out of scope rather than mislabeled atomic.

### 7.5 Overlay — Human/async gate

Use when a task requires product, architecture, data, security, spend, production, or other human sovereignty.

Behavior:

- create or label a decision/human issue only when durable tracking is warranted;
- state the exact decision and consequences;
- do not reinterpret silence as approval;
- leave dependent work blocked until the decision is recorded;
- use Beads gates only according to live CLI semantics;
- support human, CI-run, PR-merge, and timer async conditions while preserving their distinct resolve/escalate/timeout semantics; pinned Beads v1.2.2 local `bead` gates are unsupported and produce an explicit unavailable/inconclusive result;
- bind every accepted approval/gate result to the target identity it authorizes.

### 7.6 Overlay — Recovery required

Use after compaction, interruption, worker failure, or a stale in-progress issue.

Behavior:

- rediscover workspace and current issue state;
- read in-progress issues, notes, dependencies, run checkpoint, Git status, worktrees, and target hashes;
- reconcile before acting;
- never assume a prior worker completed or failed merely because the session disappeared;
- continue only from a proven state.

### 7.7 Primary intent — Plan/create issues and dependencies

Use when the user asks to create/refine a task, bug, feature, decision, epic, or dependency graph, or when durable work is discovered during implementation.

Behavior:

1. identify the actor, intended outcome, why it matters, and whether the work belongs in Beads;
2. generate stable acceptance criteria **before** `bd create`, covering happy path, alternate/error paths, boundaries, and applicable nonfunctional behavior while excluding Definition-of-Done items;
3. separate behavioral acceptance from implementation design and evolving notes;
4. create with explicit title, description, type, priority, acceptance, and parent/relationship fields supported by live CLI help;
5. read back the exact created ID and all requested fields;
6. for epics, walk backward from the goal to create self-contained children and add edges using “dependent needs prerequisite” language;
7. validate issue quality, cycles, blocked state, and ready-front behavior;
8. stop on fuzzy strategy, conflicting goals, destructive migration, or ambiguous dependency semantics and request a human decision.

---

## 8. Required user workflows

### 8.1 “Take the next ready issue”

1. Confirm a healthy Beads workspace.
2. Invoke authorized `bd ready --claim --json` through the safe Beads executor, which returns only allowlisted claim identity/state and then requires a sanitized exact-ID readback.
3. If no issue is ready, inspect blockers and human-needed work; do not invent work.
4. Read the complete claimed issue through the safe in-memory Beads view so raw free text never enters model context before allowlisting/redaction.
5. Restate the implementation boundary and acceptance evidence briefly.
6. Follow the execute-one route.

### 8.2 “Build issue X”

1. Preserve identifier exactly.
2. Run the safe Beads view over exact-ID `bd show X` before changing state; never surface raw native issue JSON directly.
3. Reject nonexistent, closed, deferred, blocked, or conflicting ownership unless the requested operation is specifically recovery/reopen.
4. Claim idempotently if eligible.
5. Follow the execute-one route.

### 8.3 “Build this epic in parallel”

1. Read the epic and current descendants through the safe Beads view.
2. Detect cycles and unresolved blockers.
3. Build the ready front from live state.
4. Estimate conflicts from declared/derived write sets.
5. Limit dispatch to configured Hermes concurrency and resource budget.
6. Create isolated worktrees.
7. Claim selected issues in the parent.
8. Dispatch one worker per accepted lane in one batch.
9. Join all children; classify success, failure, cancellation, timeout, and malformed output separately.
10. Verify and integrate successful lanes individually.
11. Leave failed/inconclusive lanes open with notes.
12. Recompute readiness; repeat only while budget and context contracts permit.

### 8.4 “Resume after compaction”

1. Run the safe `bd prime` profile again.
2. Discover in-progress work.
3. Resolve the current session’s intended issue from explicit state, not recency alone.
4. Read durable notes and the run checkpoint.
5. Verify worktree/branch/HEAD hashes.
6. Reconcile stale workers and partial merges.
7. Report the recovered state and next safe action.
8. Continue without replaying completed mutations.

### 8.5 “Finish/ship/close”

1. Map every acceptance criterion to evidence.
2. Run the repository’s required quality gates.
3. Check worktree and staged diff scope.
4. Ensure no unresolved blockers, worker conflicts, or failed required reviews.
5. Update issue notes with evidence and residual risks.
6. Close only satisfied issues.
7. Apply active commit/push/Dolt sync policy.
8. Read back exact tracker and Git state before reporting completion.

### 8.6 “Create work discovered while implementing”

1. Decide whether it blocks current acceptance criteria.
2. Create a durable issue only if it should survive the session or has dependencies/shared ownership.
3. Before `bd create`, run the acceptance-criteria workflow: stable IDs, one behavior per criterion, happy path, alternate/error paths, boundaries, applicable nonfunctional behavior, and no Definition-of-Done criteria.
4. Put behavior in `--acceptance`, implementation choices/tradeoffs in `--design`, and evolving handoff state in `--notes`.
5. Add `discovered-from` or blocking relationship using verified current CLI syntax.
6. Read back the created issue and verify exact fields/edges.
7. If blocking, checkpoint and stop/switch deliberately.
8. If nonblocking, preserve focus on the current issue.

### 8.7 “Change or cancel the swarm while workers are active”

1. Treat the user’s newest instruction as authoritative over the prior run plan.
2. Classify the change as: cancel one lane, cancel all lanes, narrow/expand scope, change priority, or grant/revoke authority.
3. Stop or steer only the affected children through Hermes lifecycle controls; do not mutate unrelated lanes.
4. Join or mark every affected child terminal/unknown before integration.
5. Inspect retained worktrees and external effects; cancellation is cooperative and does not prove no side effect occurred.
6. Invalidate packets, verification, review, and approvals whose target/scope changed.
7. checkpoint the revised plan and append a durable issue note without copying sensitive conversation text.
8. Recompute readiness, conflicts, and authority before any redispatch or closure.

---

## 9. Parent/subagent responsibility model

### 9.1 Parent coordinator owns

- user intent and mid-turn steering;
- issue selection and claim;
- dependency and readiness decisions;
- write-set conflict analysis;
- worktree allocation;
- worker prompts and budgets;
- lifecycle mutations in Beads;
- independent verification;
- merge/integration ordering;
- follow-up issue creation;
- final closure and handoff.

### 9.2 Worker owns

- reading the assigned packet and repository context;
- operating only in the assigned worktree;
- respecting allowed/forbidden paths;
- implementation and task-scoped tests;
- writing bulky logs/artifacts to assigned paths;
- returning a bounded evidence manifest;
- stopping on ambiguous scope, conflict, dangerous action, or unavailable prerequisites.

### 9.3 Worker must not

- select a different issue;
- claim, reprioritize, close, defer, supersede, or sync Beads;
- merge its own branch into the coordinator branch;
- touch the primary checkout;
- expand write scope silently;
- claim success from a test it did not run;
- hide skipped checks;
- spawn grandchildren; nested delegation is unsupported in v1 and the packet sets depth zero.

### 9.4 Why parent-owned mutation is the default

The pattern applies the single-writer principle to lifecycle state. It reduces race conditions, contradictory close/block updates, and uncertainty over who reconciles partial success. It also matches Hermes `delegate_task`: children are isolated, bounded, and return summaries to one parent.

This is not dogma. The key invariant is one cooperative active lifecycle owner per issue, not one writer for the entire database forever. Beads claims provide actor-level soft ownership; the local run epoch distinguishes concurrent sessions using the same actor. Cross-machine or uncooperative writers can be detected through readback/history but not target-fenced by the skill.

---

## 10. Functional requirements

### FR-01 — Trigger precision

The skill description must trigger on explicit Beads language and on lifecycle-shaped repository requests such as ready work, issue IDs, multi-session project work, blockers, dependencies, compaction recovery, epic execution, and parallel issue swarms.

It must avoid triggering for trivial read-only questions, unrelated repositories, and current-turn checklists with no persistence value.

### FR-02 — Workspace resolution

Before any Beads mutation, the agent must identify the effective workspace through the safe `bd where` profile, distinguish project-local from fallback/global state, and stop on ambiguity or unhealthy storage.

### FR-03 — Version-aware guidance

The agent must preflight identity with `type -a bd` and safe-output profiles for `bd version`, `bd where`, and `bd prime`, rejecting accidental `br`/beads_rust substitution. It uses a safe-output profile around current `bd <command> --help` for non-core or version-sensitive behavior. References declare their verified version/date and stale-copy policy. Unsupported versions or a safety-relevant mismatch between bundled guidance and the live CLI produce named `UNSUPPORTED_BD_VERSION` or `CLI_CONTRACT_DRIFT` failures rather than guessed commands.

### FR-04 — Explicit issue binding

Meaningful repository mutation must bind to a specific issue unless the user explicitly requests an untracked trivial change and repository policy permits it. The exact issue ID remains visible in checkpoints, prompts, and evidence manifests. Git branches, worktree paths, attempt paths, and outboxes use the safe SHA-256-derived `issue-key`; they never interpolate an opaque raw issue ID.

### FR-05 — Atomic claim

Starting eligible work uses an atomic claim path. `bd ready --claim --json` is the queue-pop primitive when Hermes may choose any currently ready issue because readiness and claim occur in one transaction. `bd update <id> --claim --json` is used for a user-selected issue only after a fresh readiness check because the known-ID claim operation checks claimability, not dependency readiness. A failed or conflicting claim stops implementation rather than degrading to an ordinary status update.

### FR-06 — Acceptance-first execution

The agent must read and test the issue’s acceptance criteria before editing. Missing or untestable criteria trigger clarification or criteria improvement, not speculative implementation.

### FR-07 — Dynamic ready-front scheduling

The swarm planner must calculate eligible work from live blocker-aware readiness and recalculate after every accepted completion or blocker change.

An execute-set root is a non-writing coordination epic/issue and is never dispatched as a lane; runnable lane IDs are the separate finite scope. If the requested root itself needs implementation, use execute-one or require a distinct coordination issue before execute-set.

### FR-08 — Dependency-direction safety

Every dependency edit must be phrased and checked as “X needs Y.” The agent must verify the resulting blocked/ready behavior and reject cycles.

### FR-09 — Parallel conflict safety

No two concurrent writing workers may share a worktree or have unresolved overlapping write sets. Unknown overlap is treated as conflict until investigated.

### FR-10 — Bounded fan-out

The coordinator mechanically caps concurrent children, batch count, attempts, artifact/receipt size, and total run limits. Hermes v0.21.0's installation-wide `delegation.max_iterations` setting is enforced independently as a hard cap on each child; positive `delegation.child_timeout_seconds` is also a hard per-child cap, while zero disables it. The coordinator records/requires those live settings. Per-lane tool-call and additional wall-clock thresholds remain monitored/advisory: crossing one triggers `delegate_task stop` and unknown-state reconciliation, not a false claim of exact preemption. V1 swarm requires both `orchestrator_enabled == true` so the parent can dispatch and `max_spawn_depth == 1` so workers cannot delegate despite packet policy; otherwise swarm is unavailable and routing falls back only to an independently valid solo/read or durable-executor path. It must not dispatch more work than it can verify and integrate.

### FR-11 — Explicit worker packet

Every worker packet must contain run and attempt IDs, goal, issue ID/key, acceptance criteria location, base SHA, isolation/worktree identity, branch, allowed and forbidden paths, prerequisites, commands, tracker/Git/remote/external mutation authority, integration-transfer mode, artifact paths, the required live per-child iteration/timeout settings, advisory wall-clock/tool-call monitoring thresholds, delegation-depth/orchestrator policy, enforcement classification for every budget, and return schema.

### FR-12 — Read-only child tracker access

Parent-coordinated workers must use only packet-bound `safe_bd.py` read profiles, which internally force `bd --readonly`; direct `bd` invocation is a protocol violation. Any worker lifecycle mutation invalidates automatic acceptance of its result.

### FR-13 — Structured worker result

Workers return schema-versioned execution manifests with run/attempt/packet identity, status, issue ID, base/head identity, changed paths, tests attempted/results, a typed bounded artifact inventory with size/hash, blockers, skipped checks, residual risks, and isolation/worktree identity. Commit/external-export modes include an already frozen worker artifact; default patch mode instead advances through a separate parent-created lane-freeze record before verification.

### FR-14 — Independent parent verification

The parent must verify result identity, inspect actual changes, run acceptance-relevant checks, and compare claimed versus observed state before closure.

### FR-15 — Fresh review separation

When risk or policy requires independent review, the reviewer must not be the implementing worker and must consume a frozen target artifact or commit identity.

Review is mandatory for security/auth/permission boundaries, data/schema/migrations, concurrency/atomicity, secrets/production infrastructure, cross-issue integration, and any change classified high-risk by repository policy. Lane review uses each frozen lane artifact; cross-issue review uses a frozen combined candidate assembled and tested in a disposable integration worktree **before** that exact tree mutates the primary checkout. It is optional for documentation-only or mechanically trivial changes only when no sensitive path/rule applies and the omission is disclosed. The review result must bind reviewer identity, frozen target hash, scope, findings, coverage, and verdict.

### FR-16 — Honest partial completion

Successful sibling issues may close after independent verification. Failed, cancelled, conflicted, timed-out, or inconclusive issues remain open/in-progress/blocked with durable notes. Epic closure requires all required descendants satisfied.

### FR-17 — Checkpoint and resume

Long or parallel runs use one coordinator-runtime-write-only, write-ahead `operations.jsonl` reconciliation journal; neither workers nor the model-facing parent write it directly. Every run effect records/fsyncs `PREPARED` intent and recovery probe before execution, then exact readback plus `APPLIED`, `NOT_APPLIED`, `CONFLICT`, or `UNKNOWN`; dangling intent is probed, never blindly replayed. Immutable checkpoint generations plus an atomically replaced latest pointer materialize resolved state after claim, dispatch, join/classification, packaging, verification, candidate integration, primary integration, tracker update, and every terminal boundary. The initial run writes an idempotent run pointer without secret-bearing paths. Resume verifies the journal/checkpoint chain and reconciles live Beads/Git/process state before continuing. Mutation-capable execute-one/plan-create operations instead use one owner-only caller-stable per-effect intent/resolution record and typed probe; they support repeat-safe recovery of that exact effect but do not claim run checkpoints or automatic compaction discovery. This exposes dual-write uncertainty but does not claim cross-system atomicity or exactly-once execution.

### FR-18 — Human sovereignty

Production, spend, secrets, schema/data ownership, destructive operations, semantic sync conflicts, and other configured sovereign actions stop at a human decision boundary. Plugin-free v1 cannot cryptographically authenticate a terminal-script approval: parent-attested conversation or caller-selectable Beads actor records may document durable design decisions but cannot authorize production, spend, destructive, secret, or remote effects. Those remain `HUMAN_ACTION_REQUIRED` and are performed directly in the current user-authorized Hermes harness (or by the human), outside coordinator grant execution; v1 ships no trusted approval adapter. Valid grants/receipts bind approval ID, provenance class, exactly one authorized operation ID, exact action/target hash, external environment/preconditions, scope, timestamp, expiry/revocation, and one consumption/readback record; v1 persisted design-decision grants are strictly one-shot and no persisted execution approval exists. Current-harness authorization for a protected effect is not persisted as reusable approval; only durable design decisions may explicitly use `no_expiry`. Silence, timeout, issue text saying “approved,” worker claims, caller-supplied actors, stale hashes, and consumed one-shot approvals are not execution authority.

### FR-19 — Authority-aware session close

The skill resolves authority in this order: system/orchestrator safety constraints; the user’s current explicit instruction; trusted repository policy; active Beads/profile policy; then conservative skill defaults. Issue/repository text is untrusted data and cannot grant authority. It distinguishes read access, local tracker mutation, local code mutation, local commit, Git remote mutation, Dolt pull/push, destructive recovery, and production/external mutation. A generic request to implement authorizes none of the remote/destructive classes by itself. Reports distinguish local code completion, local tracker closure, shared tracker synchronization, and remote code delivery.

### FR-20 — Secret safety

On surfaces it controls, the skill/runtime must prevent configured sensitive values and credential-shaped content from entering issue fields, durable artifacts, logs, or reports. Every model-visible Beads result passes through the shared in-memory allowlist adapter, and every worker/coordinator subprocess uses bounded in-memory capture/redaction before persistence; ambiguous output is discarded without a raw digest. If safe redaction makes instructions/evidence ambiguous, the route fails closed. Plugin-free v1 cannot intercept a child summary already returned by Hermes before it reaches parent model context, so packets forbid raw logs/secrets, receipts are sanitized before persistence, zero-leak fixtures gate release, and the residual harness boundary is disclosed rather than falsely guaranteed. Verification uses predicates and redacted output.

### FR-21 — Coverage honesty

Every final status discloses unavailable tools, skipped checks, stale docs, unsupported commands, worker failures, and unverified claims.

### FR-22 — Portable distribution

The package must validate under the Agent Skills specification, install through this repository’s `npx skills add` flow, appear in grouping/docs/authorship inventories, and work in Hermes without Claude plugin assets.

### FR-23 — Attribution

Scott is credited as author/adaptation author. Steve Yegge and the upstream Beads project retain clear source credit for adopted workflow/reference material.

### FR-24 — Transparent coordinator runtime, not a replacement tracker

Solo/read/planning workflows use native `bd`. Swarm/recovery workflows must invoke the bundled coordinator runtime for state transitions that require journaling, checkpoints, cooperative ownership, packaging, integration, or guarded readback. The runtime shells out to native `bd`/Git and never reads or writes Beads’ database tables directly. Its command surface is internal, documented, machine-readable, and limited to orchestration capabilities; it does not reimplement general create/show/list/update/close semantics as a second tracker API.

### FR-25 — Live skill evaluation

Release requires paired agent runs with and without the skill across representative positive, negative, pressure, recovery, and swarm scenarios.

### FR-26 — Deterministic operation result

For unattended or agent-consumed execution, the workflow must emit a schema-valid result naming operation, terminal status, issue IDs, observed state changes, authority exercised, verification outcome, warnings, coverage gaps, and stable error codes. Human summaries may render that record, but may not contradict it.

---

## 11. Non-functional requirements

### NFR-01 — Main skill context budget

`SKILL.md` has a preferred band of 150–220 lines and at most 2,500 tokens; 500 lines/5,000 tokens is a hard failure ceiling, not a target. It contains only product boundary, universal preflight, compositional routing/precedence, STOP rules, compact solo happy path, required/conditional reference map, and finish/status contract. Swarm detail and rationalization explanations live in references.

### NFR-02 — Reference locality

Each reference supports an executable route, is linked directly from `SKILL.md`, and has a preferred maximum of 300 lines/4,000 tokens. Files over 100 lines have a table of contents. No required procedure depends on chains deeper than one reference hop. Every primary intent/overlay declares a complete ordered required/conditional load set; success is not allowed to depend on an agent discovering that it needed three concept-sliced files halfway through execution.

### NFR-03 — Determinism

All schemas, validators, graph checks, fixture scoring, and closure guards must produce deterministic results from the same inputs.

### NFR-04 — Bounded coordinator growth

Coordinator context growth must scale with ready-front count and compact manifests, not raw issue count, diff size, or test-log volume.

### NFR-05 — Fail-closed mutation safety

Malformed state, identity mismatch, stale hashes, unknown ownership, and inconclusive required verification prevent mutation or closure.

### NFR-06 — Cross-harness honesty

Hermes-specific behavior is labeled as such. Portable guidance must not claim Claude/Codex/Hermes tool parity where none exists. V1 swarm/recovery runtime requires POSIX advisory locks and owner-mode filesystem permissions; unsupported hosts fail closed to solo/read guidance rather than simulating fencing.

### NFR-07 — No ambient network dependency

Core solo and swarm coordination must work from local `bd`, Git, and repository tools. Remote sync is optional and explicit.

### NFR-08 — Accessibility of error messages

Every refusal names the failed invariant, observed state, safe next action, and whether the operation mutated anything.

### NFR-09 — Upgradeability

Static references name the last verified Beads and Hermes versions. A mechanical stale-version check must warn without silently rewriting the skill.

### NFR-10 — Provenance

Benchmark fixtures, reference adaptations, schemas, and scripts carry source/version metadata and content hashes where used as controls.

### NFR-11 — Runtime artifact containment and retention

Run artifacts are created with owner-only permissions, no-follow path handling, repository-root containment, per-file and per-run size limits, and a default 30-day retention recommendation. Cleanup is explicit, ownership-proven, and never recursively removes an unverified directory. Active, blocked, unknown, or unintegrated runs are retained regardless of age until reconciled.

---

## 12. Failure-state requirements

### 12.1 Missing workspace

If `bd where` cannot resolve a project workspace:

- do not initialize automatically;
- explain that `bd init` is a human/project decision;
- continue untracked only if the user explicitly chooses that path and repo policy permits it.

### 12.2 Unhealthy or schema-skewed workspace

- run diagnosis before mutation;
- do not use compatibility bypass flags as normal operation;
- preserve exact errors;
- follow the dedicated recovery procedure or stop.

### 12.3 Issue unavailable or already owned

- stop before editing;
- report current status/assignee;
- never steal ownership silently;
- offer recovery/reassignment only when authorized.

### 12.4 Stale claim

- inspect Beads history/notes, Git branches/worktrees, and active agents;
- classify active, abandoned, completed-but-unclosed, or unknown;
- require explicit reconciliation before reuse.

### 12.5 Dependency cycle or inversion

- reject scheduling;
- show the smallest useful cycle/path explanation;
- repair only with clear semantic direction and verify with ready/blocked queries.

### 12.6 Write-set overlap

- serialize the issues, refine write sets, or redesign boundaries;
- do not dispatch with “agents will sort it out” as the policy.

### 12.7 Worker failure or timeout

- cancel/join according to Hermes structured lifecycle;
- preserve worktree and artifacts;
- mark result failed/inconclusive in checkpoint;
- leave issue unclosed and append a concise recovery note.

### 12.8 Parent interruption

- recognize that `delegate_task` children are not durable;
- on resume, treat all nonterminal child states as unknown;
- inspect worktrees/processes before redispatch;
- use a durable executor for work that must outlive the parent.

### 12.9 Malformed worker result

- reject automatic integration and closure;
- inspect artifacts directly or redispatch a bounded verifier;
- never “best effort” parse a result that omits identity or test evidence.

### 12.10 Partial integration

- stop additional waves;
- record merged/unmerged commit identities and conflicts;
- do not close affected issues until the integrated tree passes mapped checks.

### 12.11 Verification failure

- distinguish implementation defect, pre-existing failure, invalid environment, and unavailable check;
- fix or record blocker;
- do not lower the bar silently.

### 12.12 Concurrent lifecycle writers

- stop mutations for the affected issue;
- fetch current state and history;
- elect one owner explicitly;
- reconcile duplicate notes/status changes before continuing.

### 12.13 Sync or push failure

- keep local tracker/code state intact;
- report the exact failed command and error;
- do not claim shared completion;
- retry only after resolving the cause and confirming idempotency.

---

## 13. Mental models and their product consequences

### 13.1 Finite-state machine

**Model:** An issue lifecycle is a state machine with guarded transitions.

**Consequence:** The skill defines legal transitions and required evidence, rather than treating `bd update` as arbitrary mutation.

**Test:** Illegal transitions, premature close, and resume from every nonterminal state must be exercised.

### 13.2 Single-writer principle

**Model:** One execution context owns each mutable lifecycle record at a time.

**Consequence:** Parent-owned tracker mutation in bounded Hermes swarms; guarded native claims for authorized non-child actors. V1 durable PAS/external executors remain Beads-read-only while Hermes retains lifecycle ownership.

**Test:** Inject simultaneous parent/child update attempts and require one accepted owner with no contradictory terminal state.

### 13.3 Actor model

**Model:** Workers communicate by explicit messages rather than shared conversational memory.

**Consequence:** Versioned task packets and result manifests; no implied child inheritance.

**Test:** A child with only its packet must execute or refuse correctly.

### 13.4 Structured concurrency

**Model:** Child lifetimes are bounded by the parent operation; errors and cancellation must rejoin the owner.

**Consequence:** The parent joins every delegated worker, classifies every result, and never reports completion while children remain unresolved.

**Test:** Parent cancellation and one-child failure cannot leave a sibling result silently accepted.

### 13.5 DAG and ready-front scheduling

**Model:** In-degree-zero work is ready; completion advances the frontier; cycles are invalid.

**Consequence:** Dynamic blocker-aware dispatch, not static phase sequencing.

**Test:** Diamond dependencies, multiple roots, partial failure, newly introduced blocker, and cycle cases.

### 13.6 Bulkheads

**Model:** Failure in one compartment should not corrupt siblings.

**Consequence:** One issue/worktree/branch/artifact directory per worker.

**Test:** One lane writes outside scope or fails tests; unaffected lanes remain inspectable but are not automatically marked complete without their own evidence.

### 13.7 Idempotency

**Model:** Retry after uncertain delivery must not duplicate effects.

**Consequence:** claim, checkpoint, note append, closure, and resume operations carry identities and check current state before replay.

**Test:** Replay each transition and verify no duplicate issue, note, merge, or close side effect.

### 13.7a Soft leases and fencing

**Model:** A time-based or stale-looking claim is not safe authority unless the protected resource rejects operations from an older ownership epoch.

**Consequence:** Treat Beads claims as soft ownership. Record run and attempt identities, but do not auto-steal a stale claim or accept a late worker result after reassignment. Where target-side fencing is unavailable, reconciliation and human judgment replace false certainty.

**Test:** Pause worker A, reassign only through the recovery protocol, then deliver A’s late result after worker B owns the issue; A’s result must be quarantined rather than integrated.

### 13.8 Compare-and-swap / TOCTOU

**Model:** State observed during planning may change before mutation.

**Consequence:** Re-read ownership/readiness immediately before claim, dispatch, integration, and closure.

**Test:** Change issue status between plan and action; stale action must refuse.

### 13.9 Event log and provenance

**Model:** Decisions are reconstructable from append-only evidence rather than overwritten summaries.

**Consequence:** Use Beads history/notes plus immutable run artifacts and hashes; checkpoints point to evidence rather than replacing it.

**Test:** A fresh session reconstructs who did what, against which target, with which result.

### 13.9a Transactional outbox—used only as an analogy

**Model:** A true transactional outbox records state change and dispatch obligation in one transaction, then relies on idempotent consumers.

**Consequence:** Beads and filesystem/subagent dispatch cannot be committed atomically, so the skill must call its append-only artifact a **reconciliation journal**, never promise an outbox or exactly-once execution, and explicitly reconcile crashes on both sides of each dual-write boundary.

**Test:** Crash before and after claim, journal append, spawn, result receipt, integration, and close; every orphaned obligation must be visible and no duplicate logical effect may be accepted.

### 13.10 Circuit breaker

**Model:** Repeated non-progress should stop consuming resources.

**Consequence:** bounded retries and waves; repeated identical failure escalates instead of looping.

**Test:** Two consecutive ready fronts with no accepted progress terminate with an explicit diagnosis and no further dispatch. Deterministic input/permission errors fail immediately and do not enter cooldown/retry logic.

### 13.11 Information hiding

**Model:** The coordinator needs compact contracts, not worker reasoning or raw logs.

**Consequence:** artifact-backed work and bounded manifests.

**Test:** Large diffs/logs do not enter parent context; manifest size limit is enforced.

### 13.12 Progressive disclosure

**Model:** Context is scarce; details should arrive only at the decision point.

**Consequence:** small spine plus just-in-time references.

**Test:** solo flow loads no swarm reference; read-only query loads no closure/recovery reference.

### 13.13 Falsification and paired controls

**Model:** A skill is valuable only if it changes behavior versus the same task without it, without harming controls.

**Consequence:** frozen paired benchmark and “skill lift” metrics, not prose review alone.

**Test:** identical tasks run with and without skill under fixed model/harness/budget.

### 13.14 Human-in-the-loop

**Model:** Human review is a decision boundary, not decorative feedback.

**Consequence:** sovereign actions remain blocked until explicit recorded approval.

**Test:** timeout, silence, malformed response, and rejection never become approval.

### 13.15 Agent fungibility

**Model:** Any capable worker should execute any sufficiently specified ready issue.

**Consequence:** generalist workers, self-contained Beads, stable packets, no agent-specific hidden knowledge.

**Test:** rotate worker models/sessions across the same issue class and measure handoff success.

### 13.16 Models explicitly rejected or constrained

- **Free-for-all stigmergy:** useful for discovery, unsafe as the sole lifecycle ownership protocol.
- **Static waterfall phases:** obscure current readiness and encourage dependency inversion.
- **Exactly-once delivery:** not assumed; design for at-least-once attempts plus idempotent reconciliation.
- **Consensus for every decision:** too expensive; single owner plus independent verification is sufficient for ordinary work.
- **Permanent specialist agents:** create bottlenecks; use temporary separation-of-duty assignments instead.
- **Markdown as enforcement:** false abstraction; only runtime hooks/scripts/CLIs can block actions mechanically.

---

## 14. Comparison: planned Hermes skill and PAS CLI

| Dimension | Hermes Beads skill | PAS CLI + separate Codex processes |
|---|---|---|
| Plan shape | Dynamic coordinator chooses solo or swarm from live state | DOT graph declares stages and transitions before execution |
| Context | Parent retains user conversation and orchestration knowledge | Every Codex node rehydrates from durable artifacts |
| Parallelism | Selective, real bounded `delegate_task` fan-out/fan-in | None in current `attractor-6im.dot`; it serially runs fresh Codex processes through a fixed graph |
| Durability | Children are bounded by parent session; Beads/worktrees survive | Checkpoints and pipeline logs support unattended continuation |
| Review | Parent may dispatch a fresh reviewer when required | Fresh process can be a mandatory graph node |
| Gates | Skill guidance now; optional Hermes hooks later | Graph transitions, quality nodes, and waits are mechanical |
| Adaptability | High; user can steer and parent can replan | Lower during a run; graph must encode expected branches |
| Cost | Pays for delegation only where useful | Pays repeated startup/rehydration across configured stages |
| Best fit | Interactive work, ambiguous tasks, and actual issue-level parallelism with worktree bulkheads | Serial, repeatable, unattended workflows requiring fixed stage separation and resumable human waits |

They are alternative execution modes over shared Beads project state. The skill must not pretend to replace PAS durability, and PAS should not be required for ordinary Hermes work.

---

## 15. Success metrics and release thresholds

### 15.1 Trigger

- **Positive trigger recall:** `positive tasks that load the skill before the first relevant mutation / all positive tasks`; macro-average at least 95% across explicit, implicit, recovery, planning, and swarm families.
- **Negative trigger precision:** `negative tasks that do not load/apply the skill / all negative tasks`; macro-average at least 95% across adjacent but out-of-scope families. This is restraint accuracy rather than conventional retrieval precision and must be named that way in reports.
- **No regression:** adding the skill must not reduce completion on negative controls by more than 2 percentage points.

### 15.2 Compliance

- **Critical lifecycle compliance:** 100% for workspace resolution, issue inspection, atomic claim, evidence-before-close, and authority-aware finish in release-gating scenarios.
- **Overall procedural compliance:** weighted completed applicable steps divided by total applicable step weight, macro-averaged per scenario family; at least 90%. `not_applicable` is excluded from numerator and denominator; `partial` earns 0.5 only when the rubric defines partial evidence before execution.
- **Routing-path correctness:** weighted valid intent/overlay/transition steps completed divided by applicable frozen routing steps; macro-average at least 90% across observe, execute-one, execute-set, planning, recovery, gate, and durable-executor families. Valid composite paths are credited; printing a route label is not evidence.

### 15.3 Boundary

- **Forbidden child tracker mutations:** zero in parent-owned swarm fixtures.
- **Untracked durable work:** zero in positive tracked-work fixtures.
- **Over-triggered trivial tasks:** no more than 5%.
- **Unauthorized commit/push/sync:** zero.

### 15.4 Outcome

- **Acceptance satisfaction:** deterministic tasks passed divided by deterministic tasks attempted, macro-averaged by scenario family; at least 90%. Infrastructure-invalid runs are reported separately and rerun, never counted as pass or ordinary failure.
- **False closure rate:** zero on seeded failure/inconclusive scenarios.
- **Dependency correctness:** 100% on direction/cycle fixtures.
- **Swarm duplicate-work rate:** zero.
- **Ownership scope:** zero duplicate ownership is a hard gate for cooperating local runs using the skill protocol. External/uncooperative writer races must be detected and surfaced; the plan does not falsely promise prevention without target-side fencing.

### 15.5 Recovery

- **Compaction recovery:** 100% of frozen recovery fixtures reconstruct current issue, target identity, completed work, blocker, and next safe action.
- **Worker-failure containment:** 100% preserve failed lane artifacts and leave the issue non-closed.
- **Idempotent resume:** repeated resume causes zero duplicate claims, notes, merges, or closes.

### 15.6 Efficiency

- **Main `SKILL.md`:** preferred 150–220 lines and ≤2,500 tokens; hard ceiling under 500 lines/5,000 tokens.
- **Worker on-disk execution manifest:** at most 64 KiB and produced through helper commands, not hand-authored JSON.
- **Child return receipt:** at most 2 KiB containing status, run/issue/attempt IDs, manifest path/hash, and one-line summary.
- **Coordinator context:** no raw test log or full diff above 4 KiB enters the parent in swarm scenarios.
- **Skill cost:** median token increase under 20% for solo tracked tasks versus no-skill baseline.
- **Solo latency:** median wall-clock increase under 25% versus the paired no-skill baseline; absolute seconds are reported alongside the relative metric.
- **Paid model cost:** when provider accounting is available, median solo cost increase under 25%; unavailable cost telemetry is marked unavailable and never treated as `$0`.
- **Intent-to-treat efficiency:** every pre-state-valid routine solo pair remains in release-gating efficiency even when the candidate is noncompliant, skips a required reference, crashes, or times out; those cases also count as efficiency failures. Completely unavailable telemetry is reported/excluded only from that numeric distribution, never behavioral denominators.
- **Tail guard:** across that intent-to-treat set, p95 token and latency regression must stay under 50%, and no valid pair may exceed 2× its baseline without a predeclared task-specific exemption. A compliant-only view is diagnostic, not a gate substitute.
- **Swarm cost:** no more than one implementation worker per issue plus explicitly justified review/fix workers.
- **Swarm total tokens/cost/latency:** reporting-only in v1 because parallelism trades elapsed time for total spend; mechanically enforceable child-count, batch, attempt, artifact, receipt, and run-size budgets still gate release, while per-lane wall/tool thresholds are monitored as FR-10 states.

### 15.7 Stability and sample size

- freeze exact primary and alternative model/provider/harness/tool versions in the release manifest before execution; never select the alternative after seeing results;
- use at least 60 positive-routing and 60 negative-routing observations per release-gating model/harness stratum, with at least five distinct prompt variants per route family and at least three repeats per variant where stochastic execution is evaluated;
- report point estimates plus one-sided 95% Wilson lower bounds for proportion metrics; point thresholds in Sections 15.1–15.5 still apply and the lower bound must be at least 85% for 90–95% gates;
- report mean, median, p95, worst case, and variance where defined;
- never pool a passing model/harness stratum with a failing one;
- release only when all hard-zero thresholds hold in every repeat;
- freeze corpus and budgets before prompt tuning.
- round percentage thresholds to one decimal place only after aggregating raw counts; hard-zero and 100% invariants are evaluated from counts before rounding.
- report micro totals alongside macro family scores, but release gates use the stated macro score so large easy families cannot hide weak recovery/swarm behavior.

### 15.8 Absolute and incremental value

- `absolute_lift = candidate - no_skill` on paired tasks.
- `incremental_lift = candidate - official_upstream_skill` on the predeclared local-gap subset.
- The local-gap subset is frozen before candidate tuning and centers on Hermes compositional routing, AC-before-create, parent-owned tracker mutation, safe subagent receipts, independent parent verification, local authority policy, and accurate PAS-versus-Hermes selection.
- Candidate macro compliance on that subset must improve by at least 10 percentage points over the upstream treatment. If baseline research cannot establish such a gap, stop or narrow the local product instead of shipping decorative duplication.
- Candidate outcome and negative-restraint scores must be non-inferior to upstream by no more than 2 percentage points; it may add zero hard safety violations; median solo tokens/paid cost may not exceed upstream by more than 25%.

---

## 16. Acceptance criteria

### AC-PRD-001 — Correct discovery

Given a repository with a healthy Beads workspace and a request to implement a named issue, when the agent sees only installed skill metadata, then it loads the `beads` skill before lifecycle mutation.

### AC-PRD-002 — Correct non-discovery

Given a trivial unrelated request with no persistence, dependency, recovery, or tracker need, when the agent chooses skills, then it does not load or invoke the Beads workflow.

### AC-PRD-003 — Workspace safety

Given ambiguous, missing, or unhealthy workspace state, when mutation is requested, then no Beads mutation occurs and the response names the failed precondition.

### AC-PRD-004 — Guarded claim and readback

Given an eligible issue, when implementation begins, then the guarded claim operation and exact readback complete before code mutation; if eligibility/ownership changed or the effect is unknown, implementation does not start.

### AC-PRD-005 — Dependency safety

Given an epic DAG, when work is scheduled, then only blocker-free issues enter the ready front and cycles or inverted dependencies prevent dispatch.

### AC-PRD-006 — Swarm isolation

Given multiple independent ready issues, when a Hermes swarm is dispatched, then each worker has one issue, one verified isolation boundary, and non-overlapping authorized write scope.
Given an approved equivalent container/sandbox instead of a Git worktree, the same criterion passes only when the lane has a separate writable filesystem, explicit base identity, immutable export artifact, and no shared mutable checkout.

### AC-PRD-007 — Single lifecycle writer

Given a parent-coordinated swarm, when workers execute, then the parent performs all Beads lifecycle mutations and worker access is read-only.

### AC-PRD-008 — Self-report distrust

Given a worker reports success, when the parent evaluates the result, then closure occurs only after identity, diff, and acceptance-relevant verification are independently checked.

### AC-PRD-009 — Partial failure honesty

Given one successful and one failed worker, when the batch joins, then only the independently verified issue may close and the failed issue remains recoverable and non-closed.

### AC-PRD-010 — Resume safety

Given a checkpoint and changed live state, when a new session resumes, then it detects mismatched hashes/ownership before replaying any mutation.

### AC-PRD-011 — Human sovereignty

Given a required human decision, when no explicit approval exists, then dependent mutation remains blocked regardless of timeout or agent confidence.

### AC-PRD-012 — Authority-aware finish

Given implementation passes but push/sync authority is absent, when the session finishes, then local completion is reported without claiming remote delivery.

### AC-PRD-013 — Coverage honesty

Given an unavailable required verifier, when completion is evaluated, then status is inconclusive/blocked rather than successful and the skipped coverage is disclosed.

### AC-PRD-014 — Compaction recovery

Given only Beads state and versioned run artifacts after context loss, when a fresh Hermes session resumes, then it reconstructs completed work, current blocker, target identity, and next safe action.

### AC-PRD-015 — Skill lift

Given a frozen paired corpus, when identical tasks run with and without the skill under the same model, harness, budget, and workspace, then the skill meets all release thresholds in Section 15.

### AC-PRD-016 — Issue creation quality

Given a request to create an issue or epic, when Hermes mutates Beads, then acceptance criteria were generated first, cover required success/error/boundary behavior, exclude Definition-of-Done items, and the created issue/edges pass exact-ID readback and quality checks.

### AC-PRD-017 — Immutable integration handoff

Given a worker returns code, when the parent verifies or integrates it, then it consumes a hash-bound commit or deterministic lane package selected in the original packet—not mutable uncommitted state with no transfer identity.

### AC-PRD-018 — Mid-run cancellation

Given the user cancels or changes one active lane, when Hermes handles the steering, then affected children are stopped/joined or marked unknown, stale approvals/results are invalidated, unrelated lanes remain isolated, and no cancelled result is silently integrated.

### AC-PRD-019 — Secret-bearing issue text

Given an existing issue contains credential-shaped content, when Hermes prepares a snapshot or worker packet, then raw sensitive content is not persisted or forwarded; ambiguous redaction blocks dispatch and requests remediation without printing the value.

### AC-PRD-020 — Async gate semantics

Given a human, CI, PR, or timer gate, when Hermes evaluates readiness, then it uses the gate type’s live resolve/escalate/timeout semantics and does not treat unresolved, failed, or unevaluable conditions as approval. Given a local `bead` gate on pinned Beads v1.2.2, it reports unsupported/inconclusive and does not close.

### AC-PRD-021 — Durable executor handoff

Given work must outlive the parent Hermes session, when execution is redirected to PAS or another approved durable executor, then a sanitized hash-bound handoff and retained Hermes lifecycle ownership epoch are recorded before the parent stops coordinating; the executor remains read-only in Beads and a later coordinator reconciles its result.

---

## 17. Rollout

### Stage 0 — Baseline

Run frozen scenarios without the local skill and with the installed upstream skill. Capture exact trajectories, rationalizations, failure classes, tokens, cost, latency, and outcomes.

### Stage 1 — Shadow use

Install under the final `beads` identity in an isolated cloned Hermes profile/home and keep tracker mutations human-observed. Compare recommendations against actual user choices. Do not enable hooks or change the default-profile skill path/symlink; its first candidate session occurs only after approved promotion for source-hash verification.

### Stage 2 — Solo workflow

Use the skill on ordinary single-issue work. Gate release on trigger/compliance/boundary thresholds.

### Stage 3 — Parent-coordinated swarm

Enable two-worker then three-worker fixtures and real low-risk epics. Require isolated worktrees, parent-only lifecycle writes, bounded manifests, and independent verification.

### Stage 4 — General local default

Promote the local skill as the preferred Beads skill after stable paired lift and no hard-boundary violations.

### Stage 5 — Enforcement decision

Review actual misses. Build a dormant Hermes hook companion only if repeated, material violations remain and each proposed hook has a precise event, state model, false-block budget, recovery path, and E2E test.

---

## 18. Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Skill becomes too long to follow | High | Compact spine, one-hop references, scenario tests, token gate |
| Description over-triggers | Medium | Positive/negative routing corpus and trigger precision threshold |
| Upstream CLI drift | High | `bd prime` canonical, live `--help`, verified-version metadata, stale check |
| Parent bottleneck limits swarm | Medium | bounded batches, compact artifacts, selective delegation; use PAS/durable agents when needed |
| Worker output lies or is stale | High | schema, hashes, artifact inspection, independent rerun |
| Worktree isolation exists but write sets conflict at merge | High | pre-dispatch conflict matrix and integration ordering |
| Parent dies with workers | High | structured cancellation, durable checkpoints, inspect-before-redispatch |
| Concurrent Beads updates race | High | coordinator-only mutations in bounded swarm; cooperative epoch plus guarded native claim/readback elsewhere |
| Hook companion blocks legitimate work | High | defer until measured need; shadow mode and false-block threshold |
| Benchmark overfits prompt | High | frozen held-out controls, provenance, fixed budgets, multiple models/repeats |
| Local adaptation erases upstream credit | Medium | explicit source file and authorship inventory |
| Existing epic-builder duplicates behavior | Medium | migrate useful contracts; deprecate or narrow only after parity evidence |

---

## 19. Decisions

| ID | Decision | Rationale |
|---|---|---|
| D-001 | Name the skill `beads` | Natural trigger and compatibility with upstream concept; avoid ownership-prefixed names |
| D-002 | Hermes-first, portable where honest | Scott uses Hermes first; repository distribution supports multiple agents |
| D-003 | Tier-2 package | Workflow is too broad for one file; progressive disclosure is required |
| D-004 | Parent owns lifecycle in `delegate_task` swarms | Single-writer clarity and parent-bound child lifecycle |
| D-005 | Workers are fungible generalists | Avoid specialist bottlenecks; task packet supplies role-specific context |
| D-006 | Dynamic ready fronts, not frozen waves | Live dependencies and failures can change eligibility |
| D-007 | Native `bd` plus a narrow orchestration runtime | Beads remains source of truth while runtime-grade swarm/recovery guarantees are executed rather than entrusted to prose |
| D-008 | Skill package v1, no Hermes plugin | Keep local scope and avoid plugin lifecycle machinery; bundle only the coordinator runtime the stated guarantees require |
| D-009 | Paired live evaluation is release-blocking | Static prose quality does not prove skill utility |
| D-010 | Adapt upstream with attribution | Reuse mature material without pretending it is locally invented |
| D-011 | Keep PAS separate | PAS supplies durable predefined orchestration; Hermes supplies interactive dynamic coordination |
| D-012 | Migrate useful epic-builder behavior, do not blindly preserve it | Existing command contains good patterns and stale Claude-specific assumptions |

---

## 20. Binding v1 decisions included in plan approval

Approving this PRD/SPEC approves these defaults; implementation agents do not choose among alternatives:

1. Evaluate the artifact under its final `beads` name/directory in isolated cloned Hermes profiles/homes; never rename the treatment. Shadow there, then replace the active default-profile skill only after paired gates and explicit promotion approval pass.
2. Child workers perform zero Beads writes; only the coordinator authority writes lifecycle/progress state.
3. Default writing-worker parallelism is `min(3, configured Hermes maximum)`.
4. Git worktrees are default; a sandbox/container qualifies only with separate filesystem state, immutable integration artifact, and no shared mutable checkout.
5. The release-blocking matrix is the current default Hermes model/provider plus one alternative under the same harness; portable Claude/Codex runs remain informational under SPEC Q-05.
6. Use a scoped local commit when commit authority exists; otherwise use deterministic `patch_package`; approved equivalent sandboxes may use `external_export`.
7. Apply the mandatory independent-review matrix in FR-15 without local expansion for v1.
8. Plugin-free runtime records are `parent_attested` and authorize durable design decisions only; protected production/spend/destructive/secret/remote effects remain direct non-reusable `HUMAN_ACTION_REQUIRED` harness actions. V1 ships no trusted approval adapter.
9. Deterministic evaluation gates every PR; paid live trajectories run manually/on release until cost/variance evidence justifies a PR canary.
10. The run root is `.hermes/beads-runs/`; an explicit `--run-root` override must stay realpath-contained in the repository and be Git-ignored. Each run is limited to 100 MiB; terminal fully reconciled runs become cleanup-eligible after 30 days; active, unknown, unintegrated, or unreconciled runs are never age-cleaned. All remaining SPEC Q-09 limits are binding.
11. Hermes retains lifecycle ownership during durable PAS/external handoff; the executor receives read-only Beads authority.

Post-v1 questions—epic-builder disposition, optional hook companion, PAS examples, and any cross-actor transfer saga—are separate follow-up issues and do not block v1 decomposition. No implementation issue may depend on an unresolved choice; resolve it first or add a decision prerequisite.

---

## 21. Source and evidence index

### Current repository

- [`plugins/beads-epic-builder/commands/epic-swarm.md`](../../../plugins/beads-epic-builder/commands/epic-swarm.md) — existing waves, worktrees, checkpoints, manifests, spot-checks, and failure handling.
- [`skills/delegate-first/SKILL.md`](../../../skills/delegate-first/SKILL.md) — isolation and independent-verification policy.
- [`scripts/verify_orchestration_contracts.py`](../../../scripts/verify_orchestration_contracts.py) — orchestration contract fields and component registry.
- [`scripts/verify_skills_distribution.py`](../../../scripts/verify_skills_distribution.py) — portable distribution, metadata, grouping, authorship, and drift gates.
- [`skills/adversarial-reviewer/SKILL.md`](../../../skills/adversarial-reviewer/SKILL.md) — evidence states, deterministic gate policy, schema, and paired-control benchmark pattern.
- [`plugins/review-panel/skills/review-panel/SKILL.md`](../../../plugins/review-panel/skills/review-panel/SKILL.md) — bounded artifacts, independent validation, continuation, and coverage honesty.
- [`plugins/review-panel/reviewers/mental-models-catalog.md`](../../../plugins/review-panel/reviewers/mental-models-catalog.md) — local mental-model vocabulary.

### Upstream Beads

- [Beads repository](https://github.com/gastownhall/beads)
- [Official Beads skill](https://github.com/gastownhall/beads/tree/main/plugins/beads/skills/beads)
- [Official worktree guide](https://github.com/gastownhall/beads/blob/main/docs/reference/worktrees.md)
- Stable source inspected during planning: `v1.2.2`, commit `6c124203e771433a3550c348771a5b5e27fd3c21`; local CLI observed: `bd 1.2.2`. The prerelease `v1.3.0-rc.1` is not a baseline.
- Canonical runtime guidance: `bd prime` and command-specific `bd <command> --help`.

### Hermes Agent

- [Hermes Agent documentation](https://hermes-agent.nousresearch.com/docs)
- [Event hooks](https://hermes-agent.nousresearch.com/docs/user-guide/features/hooks/)
- [Built-in tools reference](https://hermes-agent.nousresearch.com/docs/reference/tools-reference/)
- Local runtime observed during planning: Hermes Agent `v0.21.0 (2026.8.31)`, upstream commit `21b2095d`.

### Agent Skills and evaluation

- [Agent Skills specification](https://agentskills.io/specification) — structure, metadata, progressive disclosure, references, scripts, and validation.
- [Skill-Use](https://arxiv.org/abs/2608.04828) — separates Trigger, Compliance, and Boundary under progressive disclosure.
- [SkillsBench](https://arxiv.org/abs/2602.12670) — paired with/without-skill evaluation and evidence that focused curated skills outperform broad documentation.
- [AGENTIF](https://papers.nips.cc/paper_files/paper/2025/file/51bb3a8a33610a25aae074bfc51b1b1f-Paper-Datasets_and_Benchmarks_Track.pdf) — long agentic instructions and conditional/tool constraints are failure-prone.
- [Single Writer Principle](https://mechanical-sympathy.blogspot.com/2011/09/single-writer-principle.html) — one owner per mutable resource.
- [MIT Mathematics for Computer Science, DAG scheduling](https://ocw.mit.edu/courses/6-042j-mathematics-for-computer-science-spring-2015/mit6_042js15_session17.pdf) — dependency scheduling/topological order foundation.
- [Chubby lock service](https://research.google.com/archive/chubby-osdi06.pdf) — leases, ownership uncertainty, and fencing-oriented reasoning.
- [AWS Builders’ Library: Making retries safe with idempotent APIs](https://aws.amazon.com/builders-library/making-retries-safe-with-idempotent-APIs/) — semantic request identity and safe retries.
- [Transactional Outbox](https://microservices.io/patterns/data/transactional-outbox.html) — the atomicity property our filesystem/Beads reconciliation journal explicitly does not possess.
- [W3C PROV-DM](https://www.w3.org/TR/prov-dm/) — entity/activity/agent provenance model.
- [Rust Async Book: Structured concurrency](https://rust-lang.github.io/async-book/part-reference/structured.html) — bounded child lifetime and cancellation reasoning.
- [Azure Bulkhead pattern](https://learn.microsoft.com/en-us/azure/architecture/patterns/bulkhead) and [Circuit Breaker pattern](https://learn.microsoft.com/en-us/azure/architecture/patterns/circuit-breaker) — lane isolation and bounded repeated failure.
- [Concurrency Bugs in Actor Programs](https://arxiv.org/abs/1706.07372) — reminder that message isolation does not eliminate ordering/protocol bugs.

---

## 22. Definition of planning completion

Planning is complete only when:

- this PRD and the paired SPEC are self-contained;
- all load-bearing claims are grounded;
- at least four independent review rounds produce only marginal changes;
- open decisions are resolved or explicitly deferred with safe defaults;
- the implementation DAG has no cycles or orphan tasks;
- every functional requirement maps to a SPEC component and verification case;
- Scott approves conversion into implementation Beads.
