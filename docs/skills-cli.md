# Install Skills with `npx skills`

`scott-cc` supports two different installation surfaces:

| What you want | Install command | What it installs |
|---|---|---|
| Skills for Codex, Hermes Agent, Claude Code, or another supported agent | `npx skills add citadelgrad/scott-cc` | Portable `SKILL.md` packages only |
| The complete Claude Code plugin | `/plugin marketplace add citadelgrad/scott-cc` | Core skills, agents, slash commands, hooks, and plugin metadata |

The distinction matters. The `skills` CLI does not translate Claude Code agents, commands, or hooks into other agent formats.

## Interactive install

Run this in a normal terminal:

```bash
npx skills add citadelgrad/scott-cc
```

The CLI will:

1. Clone the public repository.
2. Show the available skills, grouped by purpose where metadata is available.
3. Let you toggle individual skills with Space and continue with Enter.
4. Ask which detected agents should receive them. Select **Codex** and **Hermes Agent**, or any other supported targets.
5. Install into the current project by default. Add `--global` when you want the skills available across projects.

The exact agent IDs used by automation are:

- `codex`
- `hermes-agent`
- `claude-code`

Preview the full published inventory without installing anything:

```bash
npx skills add citadelgrad/scott-cc --list
```

The repository contains both core skills and skills owned by optional Claude sub-plugins, so the current inventory is larger than the 30 core skills. Interactive selection is the safest default.

## Non-interactive examples

Install selected skills globally for both Codex and Hermes Agent:

```bash
npx skills add citadelgrad/scott-cc \
  --skill acceptance-criteria \
  --skill beads \
  --skill tdd \
  --skill verified-implementation \
  --agent codex \
  --agent hermes-agent \
  --global \
  --yes
```

Install the canonical Beads skill into the current project for Hermes Agent:

```bash
npx skills add citadelgrad/scott-cc \
  --skill beads \
  --agent hermes-agent \
  --yes
```

Install every discovered skill for Codex and Hermes Agent:

```bash
npx skills add citadelgrad/scott-cc \
  --skill '*' \
  --agent codex \
  --agent hermes-agent \
  --global \
  --yes
```

That last command installs all discovered core and sub-plugin skills. Do not use `--all` casually: it targets every discovered skill and every supported agent, which is usually absurdly broad.

## Installation locations

| Agent | Project scope | Global scope |
|---|---|---|
| Codex | `.agents/skills/` | `~/.agents/skills/` |
| Hermes Agent | `.hermes/skills/` | `~/.hermes/skills/` |
| Claude Code | `.claude/skills/` | `~/.claude/skills/` |

With `skills` 1.5.22, Codex is a universal-agent target: global skills live in the canonical `~/.agents/skills/` directory. Hermes Agent receives a symlink from `~/.hermes/skills/` to that canonical copy when both are selected. Add `--copy` if symlinks are undesirable. Check `npx skills --version` if a later release reports different paths.

## Verify, update, and remove

```bash
# Show installed global skills for each agent
npx skills list --global --agent codex
npx skills list --global --agent hermes-agent

# Update global installations
npx skills update --global

# Remove selected global skills from both agents
npx skills remove acceptance-criteria tdd \
  --global \
  --agent codex \
  --agent hermes-agent
```

Restart the target agent after installing or updating if it does not refresh its skill catalog while running.

## Maintainer verification

Static distribution contract:

```bash
uv run python scripts/verify_skills_distribution.py
```

Public discovery smoke test:

```bash
npx --yes skills@latest add citadelgrad/scott-cc --list
```

Sandboxed local install smoke test:

```bash
tmp_home="$(mktemp -d)"
mkdir -p "$tmp_home/.codex" "$tmp_home/.hermes"
HOME="$tmp_home" CODEX_HOME="$tmp_home/.codex" HERMES_HOME="$tmp_home/.hermes" \
  npx --yes skills@latest add . \
  --skill beads \
  --agent codex \
  --agent hermes-agent \
  --global \
  --yes

test -f "$tmp_home/.agents/skills/beads/SKILL.md"
test -f "$tmp_home/.hermes/skills/beads/SKILL.md"
rm -r -- "$tmp_home"
```

The live public discovery command tests the latest pushed default branch. The local sandbox command tests the working tree without touching your real agent configuration.
