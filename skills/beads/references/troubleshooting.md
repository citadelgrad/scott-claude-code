---
description: >-
  Symptom-first diagnosis for Beads workspace, readiness, claims, Hermes
  delegation, isolation, checkpoints, results, integration, and sync failures.
metadata:
  tags: "beads, troubleshooting, recovery, hermes"
  source: "local Hermes-first architecture; Beads v1.2.2 compatibility baseline"
---
# Troubleshooting

Diagnose before mutation. Preserve exact sanitized errors and identities. Never
use deletion, reset, compatibility bypasses, broad cleanup, blind replay, or
closure merely to make a symptom disappear.

For swarm/recovery work, `beads_coordinator.py` capabilities are the sole
mechanical mutation path. The parent may decide semantics, call Hermes, and
perform direct authorized human actions; it must not troubleshoot by issuing
native lifecycle writes or editing run artifacts itself.

## Contents

- [Symptom table](#symptom-table)
- [Command and quality traps](#command-contract-drift)
- [State-changing and isolation failures](#state-changing-failure)
- [Verification and recovery damage](#result-and-verification-failure)
- [Escalation report](#escalation-report)

## Symptom table

| Symptom | Inspect read-only | Safe disposition |
|---|---|---|
| Database/workspace not found | executable identity, safe `bd where`, repository/Git roots | Stop. Do not run `bd init` without explicit project/human authority. |
| Wrong workspace/global fallback | canonical repository, Git common dir, Beads workspace path | `conflict`; do not retarget an existing run/checkpoint. |
| Schema skew/unhealthy Dolt | safe status/doctor output, `type -a bd`, `bd version`, storage mode | Follow dedicated recovery; no bypass flag as normal operation. |
| Dolt server mismatch | selected workspace mode, executable, process cwd/listener, runtime metadata | Touch only the exact proven stale instance; preserve other workspaces. |
| Stale claim | issue/history, run pointer, ownership epoch, worktrees, processes | Classify active/recoverable/completed-unclosed/abandoned/conflict/unknown; never steal by age. |
| No ready work | fresh `bd ready`, blockers, deferrals, gates, dependencies | Report why; do not infer same-batch unlock or invent work. |
| Dependency cycle/inversion | exact “X needs Y” edges and smallest useful cycle/path | Block dispatch; repair only with clear semantics and readback. |
| Worktree mismatch | physical path, registered worktree, branch/base/HEAD/common dir, Hermes metadata | Reject lane; a packet path or isolated context is not proof. |
| Hermes swarm unavailable | top-level `delegate_task`, depth, concurrency, automatic worktree setting | Flat delegation works with `orchestrator_enabled=false`; require available top-level `delegate_task`, `max_spawn_depth=1`, non-orchestrating child packets, and valid isolation, else use solo/read/durable route. |
| Run bootstrap incomplete | stable request mapping, manifest, root epoch, active pointer/readback, accepted checkpoints | Resume through coordinator `start-run`; forbid `front`, claim, or dispatch until pointer readback and bootstrap checkpoint 2 are accepted. |
| Dispatch response lost | `DISPATCH_PREPARED`, child inventory, transcripts, worktrees | `UNKNOWN`; retain claims and do not redispatch blindly. |
| Stop/timeout | handle/process, in-flight command evidence, worktree/external state | Cooperative stop only; classify unknown until observed. |
| Malformed/oversized result | expected identity/cardinality, receipt bytes, outbox, actual lane | Reject/quarantine; inspect safely, never best-effort parse. |
| Worker says tests passed | immutable artifact, log hashes, actual diff, fresh parent command | Self-report is a claim; rerun or return inconclusive. |
| Checkpoint hash mismatch | request/run mapping, journal chain, accepted generations, `latest.json` | `conflict`; preserve bytes, never rewrite history broadly. |
| Orphaned worktree | run/attempt/epoch, branch/base/HEAD, result/freeze/integration state | Retain until ownership and integration state are fully reconciled. |
| Candidate conflict/failure | predecessor, ordered lane freezes, candidate hash, test/review target | Preserve candidate and lanes; primary unchanged; no auto-resolution. |
| Integrated but issue open | exact primary tree and issue evidence | Reverify current integrated target; guarded close without reintegration. |
| Git/Dolt push partially fails | local and exact remote state, prepared operation, authority | Do not claim shared delivery or retry until target state is classified. |
| Human/gate timeout | gate type and exact target, live help, typed probe | Timeout is not approval; only timer semantics apply to timer gates. |
| Local `bead` gate | pinned version/live gate behavior | Unsupported/inconclusive in v1; block close. |
| Sensitive output | controlled surfaces and sanitized failure record | Discard raw value; block if meaning is ambiguous; do not persist a raw digest. |

## Command-contract drift

Run `bd prime` after a new/compacted session and use safe command-specific
`bd <command> --help` before relying on uncertain syntax. This skill targets Go
/Dolt `bd`, not `br`/beads_rust. If installed behavior conflicts with a
safety-relevant pinned claim, return `UNSUPPORTED_BD_VERSION` or
`CLI_CONTRACT_DRIFT`; do not guess or silently rewrite the reference.

Pinned identities used by this package are Beads 1.2.2 at
`6c124203e771433a3550c348771a5b5e27fd3c21` and the approved Hermes 0.21.0
snapshot at `21b2095d00a98b8ad7b5c60b10587619c852cdb8`.

## Quality-command traps

`bd preflight` in Beads 1.2.2 is an upstream-Beads contributor checklist, not a
generic host-repository test gate. Its `--fix` is not implemented. Warnings or
skips do not prove the host build passed. Run the repository’s own required
commands separately.

Judge `bd lint --json` from its reported warning/error totals and exact scope,
not process exit status alone. Likewise, native `bd close` may warn and continue
when some gate evaluation fails; require a typed affirmative gate probe first.

## State-changing failure

For any command/tool that may have crossed a mutation boundary:

1. Do not retry from the error message alone.
2. Locate its stable operation ID and write-ahead `PREPARED` record.
3. Probe the exact Beads/Git/filesystem/Hermes/remote target.
4. Resolve `APPLIED`, `NOT_APPLIED`, `CONFLICT`, or `UNKNOWN`.
5. Record exact readback and checkpoint before another effect.

Missing output and timeouts are insufficient observation, not proof of no
mutation. The reconciliation journal is not a transaction or exactly-once
queue.

## Isolation failure

Reject parallel writing when two lanes share a worktree/branch, touch the
primary checkout, overlap allowed paths, alias paths by case or symlink, have
unknown write sets, or rely on an automatic Hermes worktree different from the
packet. Serialize or create new verified boundaries. Do not “fix” a collision
by widening both scopes.

## Result and verification failure

Separate:

- implementation defect;
- introduced test failure;
- proven pre-existing failure;
- invalid environment;
- unavailable required check;
- stale/mismatched target;
- protocol violation.

Only current target-bound affirmative evidence passes. Missing log/hash,
changed branch, out-of-scope diff, self-review, wrong candidate hash, or
unavailable verifier yields reject/inconclusive. Preserve the attempt for
recovery and leave its issue non-closed.

## Recovery damage

A stale/missing `latest.json` can be rebuilt only from the last valid
`CHECKPOINT_ACCEPTED` event naming an extant exact generation. Stray unaccepted
generations remain evidence. Corrupt accepted hashes, missing generations,
interior journal damage, and ambiguous final fragments stop mutation.

Do not reset an ownership epoch, auto-steal an expired lease, truncate a journal,
remove an unknown run, or recursively delete `.hermes/beads-runs`. Active,
unknown, conflicted, unintegrated, and unreconciled artifacts are retained
regardless of age.

## Escalation report

For every non-success report:

- failed invariant and exact target identity;
- observed sanitized state and evidence path/hash;
- whether any effect is applied, not applied, conflicting, or unknown;
- preserved run/issue/attempt/epoch/worktree/candidate identities;
- skipped/unavailable checks and authority boundary;
- one next safe action.

Use `blocked`, `inconclusive`, `conflict`, `UNKNOWN`, or
`HUMAN_ACTION_REQUIRED`; never soften uncertainty into success.
