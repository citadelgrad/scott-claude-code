# Quick Start

## Install

### Skills for Codex, Hermes Agent, Claude Code, and others

```bash
npx skills add citadelgrad/scott-cc
```

Use Space to select skills, then choose target agents such as Codex and Hermes Agent. Their automation IDs are `codex` and `hermes-agent`.

For a non-interactive global install into both:

```bash
npx skills add citadelgrad/scott-cc \
  --skill acceptance-criteria \
  --skill beads \
  --skill tdd \
  --agent codex \
  --agent hermes-agent \
  --global \
  --yes
```

This route installs skills only. See [docs/skills-cli.md](docs/skills-cli.md) for all options, locations, verification, and removal.

### Complete Claude Code plugin

```bash
/plugin marketplace add citadelgrad/scott-cc
```

Use the plugin route when you also want Claude-specific agents, slash commands, hooks, and sub-plugins.

## What You Get

**Core Plugin (scott-cc)**
- 8 slash commands (concurrency-atomicity, delegate-first, gha, google-standard, handoff, polyglot-idiom, security-cheatsheet, thermo-nuclear)
- 7 AI agents (api-debugger, backend-architect, deep-research-agent, frontend-architect, refactoring-expert, requirements-analyst, system-architect)
- 30 skills (adversarial-reviewer, beads, delegate-first, grill-me, tdd, acceptance-criteria, verified-implementation, python-simplifier, typescript-simplifier, context7, thinking-in-systems, emergent-behavior, and more)

**Sub-Plugins**
- beads-epic-builder - Plan, build, and swarm beads epics (2 agents, 2 commands)
- browser-automation - Browser testing & validation (2 agents, 2 skills)
- research-tools - Learning guides, tech stack research (3 agents, 1 skill)
- security-suite - Security advisory and scanning (2 agents, 1 skill)
- performance-optimization - Performance engineering (1 agent)
- mutation-testing - Mutation testing suite (5 agents, 1 skill)
- review-panel - Multi-persona adversarial code and design review
- variant-explorer - Parallel blind-builder implementation exploration
- triage - Foundry-resident detect, reproduce, fix, and gate loop

## Usage

```bash
# Plan a feature into a beads epic
# Just describe what you want to build - epic-planner agent activates automatically

# Build an epic sequentially
/build-feature <epic-id>

# Build an epic with parallel workers
/epic-swarm <epic-id>

# Code quality
/python-simplifier
/typescript-simplifier

# Keep implementation noise out of the main thread
/delegate-first

# Generate a compact reload note before clearing context
/handoff
```

## Hooks Need a Real Plugin Install

The 5 hooks (`terminal-bell`, `toon-post-hook`, `prefer-modern-tools`, `data-layer-guard`, `post-compaction`) only activate when Claude Code loads this repo as an installed plugin — that's what makes `${CLAUDE_PLUGIN_ROOT}` resolve in `hooks/hooks.json`. Installing through `npx skills`, or cloning the repo without plugin wiring, does not activate them. See the note in [README.md](README.md#hooks-5) for how to wire hooks up manually from a plain clone.

## Links

- GitHub: https://github.com/citadelgrad/scott-cc
- README: Full documentation
- PUBLISHING.md: How to publish updates
