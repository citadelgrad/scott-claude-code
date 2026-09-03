---
description: >-
  Execute one Beads issue from identity preflight and guarded claim through
  scoped implementation, current evidence, guarded close, and truthful handoff.
metadata:
  tags: "beads, solo, claim, verification, closure, handoff"
  source: "Beads 1.2.2 live help and frozen Hermes Beads v1 plan"
  verified: "2026-09-03"
---

# Solo execution

This is the complete one-hop route for one issue. Verified against Beads
`1.2.2`. Command examples name native semantics; invoke them through the
package's closed safe Beads/direct-operation transport when that runtime is
present. Its live help and schemas define exact profile names and result fields.
If a required transport, stable operation identity, or sanitized readback is
unavailable, return `blocked` or `inconclusive`; prose does not supply the
missing guarantee.

## Contents

- 1. Preflight and select
- 2. Claim and bind the mutation boundary
- 3. Implement and checkpoint
- 4. Verify and review
- 5. Close with guards
- 6. Finish under authority
- Terminal outcomes and pressure checks

## 1. Preflight and select

Before the first tracker or implementation write:

1. Capture physical repository root, Git common directory, branch, `HEAD`, and
   `git status --short`. Do not expose secret-file contents.
2. Resolve `type -a bd`; require Go/Dolt Beads `1.2.2` or an explicitly reviewed
   compatible contract.
3. Run safe `prime` and `where` reads; confirm the workspace belongs to the
   intended repository and is healthy enough for the required reads/writes.
4. Resolve active authority separately for local tracker mutation, local code
   edits, commit, Git remote, Dolt pull/push, destructive recovery, and external
   effects.
5. Detect stale claims, partial direct-operation records, unresolved gates, or
   unknown prior effects. Recovery wins over new execution.

### Named issue

- Preserve the supplied ID byte-for-byte; never repair or normalize it.
- Pass it as a single argv value. For an ID that resembles a flag, use the
  live-supported `bd show --id=<exact-id> --json` form.
- Read the sanitized complete exact issue and acceptance criteria first.
- Require stored status `open`, no conflicting assignee, meaningful testable
  acceptance criteria, and current membership in blocker-aware readiness.
  Status alone does not prove readiness.
- Immediately before claim, re-read exact status, assignee, blockers, gates,
  and ready membership.

### Queue-pop

Do **not** invoke unknown-target native `bd ready --claim --json` on the direct
route. It is safe only inside a coordinator operation that durably records a
caller-stable write-ahead identity before mutation and can reconcile the exact
selected issue after interruption. Without that capability, an interruption
after claim but before the selected ID is persisted can make replay claim a
second issue.

The direct route instead runs a sanitized read-only `ready` profile, preserves
one returned ID byte-for-byte, then treats it as a known issue: immediately
re-read its exact status, assignee, blockers, gates, and ready membership and
execute the guarded write-ahead known-ID claim below. Another actor may win
between selection and claim; that is a normal conflict, never a reason to use a
generic status update or silently select again. If postclaim readback fails or
eligibility changed, preserve the known operation and claimed ID, reconcile or
compensate under explicit release/reassignment authority, and do not implement.
If no issue is returned, inspect blockers/human-needed work; do not invent one.

## 2. Claim and bind the mutation boundary

For a named issue, execute the stable, write-ahead known-ID claim operation
whose native effect is `bd update <exact-id> --claim --json`. The native command
atomically claims a claimable known issue, but does **not** prove dependency
readiness. Never replace a failed claim with a generic status update.

After a known-ID claim, or a coordinator-owned recoverable atomic queue claim,
require all of the following from exact readback:

- returned and read-back IDs equal the requested/selected ID exactly;
- stored status is `in_progress`;
- assignee equals the expected Beads actor;
- blockers and required gates remain clear;
- the issue was eligible at the required checks;
- the caller-stable operation effect is resolved, not unknown;
- any cooperative run identity used by the installed runtime still matches.

Same-actor claim idempotence is actor-level soft ownership, not proof that this
Hermes run owns the work. Conflict, changed eligibility, a stale target, or an
unknown effect stops before implementation. Preserve the claim/readback and
route to reconciliation; do not blindly reclaim or release it.

**Mutation boundary:** successful claim and exact readback precede every durable
implementation filesystem write. This includes creating a new untracked source
file, editing an existing file, generating code, or staging content—not merely
changes Git already tracks. Read-only inspection may occur before claim.

Freeze the base branch/`HEAD`, initial status, exact issue/AC identity, allowed
scope, and planned evidence before editing.

## 3. Implement and checkpoint

1. Map every stable AC ID to an observable and planned authoritative check.
2. Inspect relevant code, then write only within the authorized issue scope.
3. Recheck issue/target identity after material external or lifecycle change.
4. Append durable notes only for decisions, progress, blockers, or next actions
   a fresh agent needs. Do not paste secrets, raw logs, or conversational noise.
5. Put durable discoveries through the issue-creation route. If they block an
   AC, checkpoint and stop/switch deliberately; otherwise preserve focus.
6. On a human/async gate or unknown prior effect, transition to gate/recovery;
   do not improvise a pass.

Keep implementation claims separate from observed Git state. A clean worktree
does not prove that a stale claim is abandoned; a dirty worktree does not prove
that the current run owns it.

## 4. Verify and review

Use the detailed [verification and closure contract](verification-and-closure.md)
and the transition rules in [issue lifecycle](issue-lifecycle.md). This section
is the one-issue checklist, not a replacement for those maintainer references.

Build an AC-to-evidence ledger for the current target:

| AC ID | Observable | Check | Target identity | Fresh result | Gap |
|---|---|---|---|---|---|
| stable ID | exact behavior | smallest authoritative command/inspection | branch + `HEAD`/tree or artifact hash | passed/failed/unsupported/unavailable/not applicable | explicit |

Run criterion-specific checks and every repository-required integration gate.
Inspect final status/diff and identify unrelated changes. Classify failures as
implementation defect, introduced regression, pre-existing defect, invalid
environment, or unavailable verifier; do not erase a failure because it also
exists at base.

A distinct reviewer against a frozen target is mandatory for security/auth or
permissions, data/schema/migrations, concurrency/atomicity, secrets/production
infrastructure, cross-issue integration, and repository-classified high risk.
A non-sensitive documentation or mechanical change may skip fresh review only
when policy allows it and the omission is disclosed.

A required verifier that cannot run yields `blocked` or `inconclusive`, names
the exact missing coverage and next action, and prevents close. Never silently
substitute a weaker check. A zero exit code counts only when the check proves
the named observable.

## 5. Close with guards

Immediately before close, re-read and require:

- exact issue, workspace, actor, branch, and target identity still match;
- every AC has affirmative current evidence;
- repository-required gates passed against this target;
- no active blocker or conflicting ownership exists;
- every applicable human/async gate has a typed affirmative target-bound result;
- required independent review passed against the unchanged frozen target;
- local close authority is active.

Append a concise evidence note only when policy requires durable handoff. Then
use dedicated `bd close <exact-id> --reason <reason> --json` through the guarded
stable operation. Do not use `bd update --status=closed`, and do not use
`--force` to bypass a blocker, pinned item, or unsatisfied gate.

Native `bd close` is necessary but insufficient: Beads can close an issue that
has no AC/evidence, and some gate-evaluation failures may only warn. Accept
closure only after the workflow guards above and exact post-close readback show
the expected ID, `closed` state, close reason/actor evidence, and resolved
operation causality. Unknown or mismatched close effect requires recovery, not
a second blind close.

## 6. Finish under authority

Resolve profile, branch, and remote authority with [Git and Dolt
boundaries](git-and-dolt-boundaries.md). The route taxonomy remains in
[operating modes](operating-modes.md).

Resolve each action independently. Generic implementation authority does not
imply commit, Git push, Dolt pull/push, destructive, production, spend, secret,
or external-effect authority. Perform only explicitly authorized actions and
read back each exact target.

Report these milestones separately:

1. code changed locally;
2. tracker closed locally;
3. tracker synchronized to a Dolt remote;
4. code delivered to a Git remote.

Without remote authority, report the first two observed local milestones only.
Do not run `bd sync`; released `1.2.2` has no usable command by that name. A
successful `bd dolt push` process is not remote proof when no remote is
configured or the remote head cannot be read back.

## Terminal outcomes

Use one honest outcome and include exact target, mutations performed, authority
exercised, evidence, gaps, and next safe action:

- `success_local` — required local implementation/closure evidence is complete;
  no unperformed remote milestone is implied.
- `success_shared` — separately read-back authorized Git/Dolt remote milestones
  are complete.
- `blocked` — a known prerequisite, authority boundary, or failed check remains.
- `conflict` — observed target, ownership, eligibility, or state contradicts
  intent.
- `inconclusive` — required evidence or external effect cannot be established.
- `workspace_error` — executable/workspace/health identity failed before safe
  mutation.
- `human_required` — a protected decision or action remains human-owned.

## Pressure checks

- “It is obvious” does not waive exact issue/AC inspection.
- “I will claim after a quick edit” violates the mutation boundary, including
  for new untracked files.
- “The claim succeeded” does not waive readiness and exact readback.
- “Tests passed earlier” is stale unless bound to the current target.
- “The check is unavailable but the diff is small” is inconclusive, not success.
- “Almost done” does not justify close after a failed gate.
- “Ship it” does not grant push, sync, force, or production authority.
