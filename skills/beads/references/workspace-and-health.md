---
description: >-
  Resolve Beads executable, repository, workspace, storage mode, health, and
  version before any lifecycle mutation.
metadata:
  tags: "beads, workspace, health, dolt, preflight"
  source: "Beads 1.2.2 live help and frozen Hermes Beads v1 plan"
  verified: "2026-09-03"
---

# Workspace and health

Verified against Beads `1.2.2`. Use this reference when workspace identity,
storage health, or command compatibility is abnormal. Every model-visible Beads
read must use a closed `safe_bd.py` profile. If the adapter or required profile
is unavailable, return `blocked` or `inconclusive`; there is no native-output
fallback. Inspect the installed registry/help rather than inventing profile
names or output fields.

## Contents

- Identity preflight
- Workspace ownership decision
- Health ladder
- Failure handling
- Flag and version semantics

## Identity preflight

Before any Beads mutation, capture without changing state:

1. physical repository root: `git rev-parse --show-toplevel`, then realpath;
2. physical Git common directory: `git rev-parse --git-common-dir`, resolved
   relative to the worktree and then realpath;
3. every `bd` resolution from `type -a bd`, followed by the safe version profile;
4. session guidance from the safe prime profile after a new session, compaction,
   or material contract drift;
5. effective workspace from the safe read-only workspace/where profile;
6. the intended repository/worktree identity supplied by trusted context.

Require the Go/Dolt `bd` executable from `gastownhall/beads`. `br` or
beads_rust is not an interchangeable fallback. An alias, wrapper, path, or
version different from the approved executable must be explained before use.

## Workspace ownership decision

Canonicalize and compare the returned `path`, `database_path`, and `prefix`
with the intended physical repository. Determine and disclose why it resolved:

- project-local `.beads`;
- ancestor discovery from a subdirectory;
- linked worktree sharing the primary checkout's Beads workspace;
- redirect/routing configuration;
- `BEADS_DIR` or an explicit `--db` override;
- `--global` workspace;
- external or cloud volume.

A linked Git worktree may legitimately share the primary checkout's tracker.
A separate Git branch does not imply a separate Beads database. Sharing must be
reported, not hidden.

If the path is missing, ambiguous, outside the intended repository ownership,
or selected by an unexplained override, return `workspace_error`, name the
observed and expected identities, and perform zero Beads mutation. Never drift
to a nearby or global database for convenience.

## Health ladder

Run the smallest supported read-only checks needed for the route:

1. the safe read-only status profile for database summary and ready/blocked counts;
2. the safe read-only Dolt-status profile for storage mode, path, and reachability;
3. a minimal required read such as exact `show` or `ready` through the safe
   transport;
4. the safe read-only doctor profile only when the installed mode supports useful
   structured diagnostics, then inspect its sanitized content rather than exit
   status alone.

Classify each check as **affirmative**, **degraded/unsupported**, or **failed**.
In embedded mode, `mode: embedded` with `server_running: false` is normal: the
engine runs in process. On the pinned build, agent doctor may exit zero while
saying the mode is unsupported or may emit plain text despite `--json`; that is
reduced diagnostic coverage, not affirmative health.

For a managed or external server, require the configured endpoint, database,
server identity, and reachability to agree. A missing cloud/external volume,
wrong server, schema failure, or unreadable database is a failed precondition.

## Failure handling

| Failure | Safe result |
|---|---|
| No workspace | `workspace_error`; zero mutation; state that `bd init` is a human/project decision |
| Wrong/ambiguous workspace | `workspace_error`; report both identities and the selecting override if known |
| Schema skew | Preserve the exact error; use explicit recovery guidance; no normal-work bypass |
| Server/version mismatch | Stop affected operations and diagnose the selected executable/storage mode |
| Required read unavailable | `blocked` or `inconclusive`; disclose the missing observation |

Do not run `bd init`, `doctor --fix`, `init --force`, or
`--ignore-schema-skew` to make ordinary work proceed. Recovery and destructive
repair need separate authority, backups where applicable, and exact readback.

## Flags and drift

- `--readonly` blocks write operations. Use it for observation and worker reads.
- `--sandbox` disables Dolt auto-push; it does **not** prevent local writes.
- `--db` and `--global` select identity; never add either silently.
- `--json` requests structure but does not guarantee every mode actually emits
  JSON; parse and classify the observed codec.

These are underlying native semantics for `safe_bd.py` profiles, not permission
to invoke native `bd` and expose its output. Prime, help forwarded to the model,
workspace, health, issue, ready/blocker, gate, history, and mutation readback all
cross the same mandatory allowlist/redaction/size/codec boundary.

The verified baseline is Beads `1.2.2`. Compare safety-relevant live help with
the frozen manifest. Unsupported version means `UNSUPPORTED_BD_VERSION`;
changed mutation semantics means `CLI_CONTRACT_DRIFT`. Both stop the affected
mutation until reviewed. Never guess replacement syntax.
