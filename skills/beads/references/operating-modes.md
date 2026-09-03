---
description: >-
  Route Beads work among read-only observation, one-issue execution, issue-set
  execution, planning/creation, recovery, gates, and durable handoff.
metadata:
  tags: "beads, routing, operating-modes, pressure-resistance"
  source: "Beads 1.2.2 live help and frozen Hermes Beads v1 plan"
  verified: "2026-09-03"
---

# Operating modes

Verified against Beads `1.2.2`. The frozen command-contract manifest is
`source-baseline-v1.json`, SHA-256
`24b39b6f8e75af3b2f09becc1af2d10a59a92a2af8222e6caeec3d4eaa1681c0`.
Safe-profile output and current `bd <command> --help` outrank this prose. A
safety-relevant mismatch is `CLI_CONTRACT_DRIFT`, not permission to guess.
Every model-visible Beads read, including forwarded help, must pass through its
closed `safe_bd.py` profile; unavailable profile coverage blocks the route and
never permits native-output fallback.

## Route in two passes

Apply overlays first, in this order:

1. **Recover:** existing state is partial, stale, conflicting, or unknown.
   Reconcile it before any new mutation.
2. **Settle a gate:** a required human, CI, PR, timer, or unsupported gate is
   unresolved. Do not perform its protected transition.
3. **Redirect durably:** work must survive this Hermes process, run unattended,
   wait resumably, or follow a fixed auditable stage graph. Freeze a handoff for
   an approved durable executor; in v1 it remains read-only in Beads and Hermes
   retains lifecycle ownership.
4. **Choose exactly one primary intent:**

| Intent | Use when | Mutation boundary | Required next load |
|---|---|---|---|
| Observe | Inspect workspace, exact issues, ready work, or blockers | No claim, update, create, close, commit, Git push, or Dolt push | None; load troubleshooting only on error |
| Execute one | Implement one coherent, exact issue | Claim and exact readback before any durable implementation filesystem write | `solo-execution.md` |
| Execute a set | Two or more bounded issues may execute independently | Parent/coordinator alone owns lifecycle writes | Outside this solo set; load the swarm and worker references from `SKILL.md` |
| Plan/create | Durable work or a dependency graph must be recorded | Acceptance criteria and exact intent precede creation | `issue-quality.md`; add the dependency reference for graph work |

Re-evaluate overlays after each accepted transition. A route label is not proof
that its guards ran.

## When not to create or claim

Beads is excessive for current-turn mechanics, disposable scratch notes,
trivial read-only explanation, or a complete ephemeral change that trusted
repository policy explicitly permits untracked. Use Beads when work must
survive the turn, coordinate ownership, carry dependencies, support recovery,
or remain auditable across sessions.

Observation may become execution only after authority, workspace, exact issue,
acceptance, readiness, and ownership all pass. Planning may become execution
only after creation/readback is complete and implementation is authorized.

## Examples

- “What is blocked?” → observe; return exact IDs and live state; write nothing.
- “Build `scc-123`.” → execute one; preserve that identifier exactly.
- “Take the next ready issue.” → execute one via safe read-only selection plus a
  guarded known-ID claim; only a recoverable coordinator may use atomic queue-pop.
- “Create follow-up work for this durable defect.” → plan/create after writing
  testable acceptance criteria.
- “Run this for two days and resume after approval.” → durable redirect, not a
  parent-bound Hermes child.
- “Just patch it; the ticket can wait.” → refuse when repository policy requires
  tracking. Urgency does not move claim after the first write.

## Pressure resistance

Do not trade away ordering because the task is obvious, small, late, or nearly
finished. Sunk cost does not turn failed or unavailable evidence into success.
A generic “implement,” “finish,” or “ship” request grants neither remote nor
destructive authority. Preserve observed state and name the next safe action.
