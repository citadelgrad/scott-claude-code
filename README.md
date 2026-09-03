# Scott's Agent Skills and Claude Code Setup

Portable agent skills plus a modular Claude Code plugin suite for productive development. The core plugin provides **8 slash commands**, **7 specialized AI agents**, **30 skills**, **6 hooks**, and **2 stored templates that produce 3 project artifacts**. Specialized sub-plugins add beads epic workflows, browser automation, mutation testing, multi-persona code review, and more.

Created and maintained by **Scott Nixon ([@citadelgrad](https://github.com/citadelgrad))**. See [Skill Authorship and Provenance](SKILL-AUTHORSHIP.md) for the per-skill inventory and [review-panel credits](plugins/review-panel/CREDITS.md) for original creators of vendored and adopted work.

## Quick Install

Install selected skills for Codex, Hermes Agent, Claude Code, or another supported agent:

```bash
npx skills add citadelgrad/scott-cc
```

The interactive installer lets you choose individual skills and target agents. For exact non-interactive commands, paths, verification, and the skills-only boundary, see **[Install Skills with `npx skills`](docs/skills-cli.md)**.

Install the complete Claude Code plugin, including agents, slash commands, and hooks:

```bash
/plugin marketplace add citadelgrad/scott-cc
```

`npx skills add` installs portable skills only. It does not install the Claude-specific agents, slash commands, hooks, or sub-plugin runtime wiring.

## At a Glance

| Type | Count | Names |
|------|------:|-------|
| Commands | 8 | `delegate-first`, `gha`, `handoff`, `security-cheatsheet`, `thermo-nuclear`, `google-standard`, `polyglot-idiom`, `concurrency-atomicity` |
| Agents | 7 | `api-debugger`, `backend-architect`, `deep-research-agent`, `frontend-architect`, `refactoring-expert`, `requirements-analyst`, `system-architect` |
| Skills | 30 | `init`, `acceptance-criteria`, `beads`, `cli-design`, `delegate-first`, `grill-me`, `adversarial-reviewer`, `tdd`, `python-simplifier`, `typescript-simplifier`, `go-simplifier`, `rust-simplifier`, `swift-simplifier`, `karpathy-guidelines`, `property-based-testing`, `verified-implementation`, `context7`, `context-file-optimizer`, `c4-diagram`, `writing-about-engineering`, `writing-skills-excellence`, `pas-pipeline`, `reck-factory`, `thinking-in-systems`, `emergent-behavior`, `skillopt-sleep-learned`, `thermo-nuclear`, `google-standard`, `polyglot-idiom`, `concurrency-atomicity` |
| Hooks | 6 | `terminal-bell` (Stop), `toon-post-hook` (PostToolUse), `prefer-modern-tools` (PreToolUse), `data-layer-guard` (PreToolUse), `post-compaction` (SessionStart after compact/clear), `review-panel-session-identity` (SessionStart) |
| Templates | 3 | `.pre-commit-config.yaml`, `CLAUDE.md`, `AGENTS.md` |
| Sub-plugins | 9 | `beads-epic-builder`, `browser-automation`, `research-tools`, `security-suite`, `performance-optimization`, `mutation-testing`, `review-panel`, `variant-explorer`, `triage` |

---

## Commands (8)

| Command | Description |
|---------|-------------|
| `/scott-cc:delegate-first` | Keep the main conversation clean by forking implementation work to sub-agents. |
| `/scott-cc:gha` | Debug failing GitHub Actions runs and audit workflow YAML. Fetches logs via `gh` CLI, analyzes errors, suggests fixes. |
| `/scott-cc:handoff` | Generate a compact session handoff with git state, active work, key files, and concrete next actions. |
| `/scott-cc:security-cheatsheet` | Look up OWASP security cheatsheets by topic. Comprehensive security reference for common vulnerabilities and mitigations. |
| `/scott-cc:thermo-nuclear` | Zero-mercy structural-simplification review grounded in Cursor's thermo-nuclear-code-quality-review doctrine. Biases toward ambitious rewrites over preserving imperfect-but-working code. |
| `/scott-cc:google-standard` | Review against Google's published Standard of Code Review. Favors approving once a change definitely improves code health, even if imperfect, using the Nit:/blocking distinction. |
| `/scott-cc:polyglot-idiom` | Per-language idiom review for Java, C++, C#, Ruby, or PHP. Excludes Python, TypeScript, Go, Rust, and Swift, which have dedicated simplifier skills. |
| `/scott-cc:concurrency-atomicity` | Concurrency-correctness review for race conditions, TOCTOU, deadlock/lock-ordering, and transactional atomicity, grounded in real CWE reference entries. |

---

## Agents (7)

### Engineering

| Agent | Description |
|-------|-------------|
| `backend-architect` | Design reliable backend systems with focus on data integrity, security, and fault tolerance. |
| `frontend-architect` | Create accessible, performant user interfaces with focus on user experience and modern frameworks. |
| `system-architect` | Design scalable system architecture with focus on maintainability and long-term technical decisions. |

### Analysis

| Agent | Description |
|-------|-------------|
| `deep-research-agent` | Specialist for comprehensive research with adaptive strategies and intelligent exploration. |
| `requirements-analyst` | Transform ambiguous project ideas into concrete specifications through systematic requirements discovery and structured analysis. |

### Debugging & Quality

| Agent | Description |
|-------|-------------|
| `api-debugger` | Expert debugger for APIs, Python backends, and JavaScript/TypeScript frontends with integrated browser testing via Playwright MCP. |
| `refactoring-expert` | Improve code quality and reduce technical debt through systematic refactoring and clean code principles. |

---

## Skills (30)

### Project Setup

| Skill | Description |
|-------|-------------|
| `init` | Interactive project scaffolding. Detects what already exists, presents a menu, and sets up only what you select: beads (`bd init`), `CLAUDE.md`, `AGENTS.md` symlink, `.envrc`, `Makefile`, and pre-commit hooks. |
| `acceptance-criteria` | Generate testable acceptance criteria before creating beads issues or planning implementation work. |
| [`beads`](skills/beads/README.md) | Hermes-first durable issue coordination for Go/Dolt Beads (`bd`), with guarded lifecycle changes, bounded orchestration, recovery, and exact-readback completion. |

### Code Quality

| Skill | Description |
|-------|-------------|
| `python-simplifier` | Simplifies and refines Python code for clarity, consistency, and maintainability. Applies KISS principles, Pythonic patterns, and framework best practices. Use when reviewing or refactoring Python code. |
| `typescript-simplifier` | Simplifies and refines TypeScript/JavaScript code for clarity, consistency, and maintainability. Applies KISS principles, modern ES features, and framework best practices. Use when reviewing or refactoring TS/JS code. |
| `go-simplifier` | Simplifies Go code using idiomatic error handling, interfaces, concurrency patterns, and standard-library conventions. |
| `rust-simplifier` | Simplifies Rust code using idiomatic ownership, error handling, traits, iterators, and concurrency patterns. |
| `swift-simplifier` | Simplifies Swift code using idiomatic optionals, protocols, value semantics, concurrency, and API design. |
| `cli-design` | Design patterns and conventions for building agent-compatible CLI tools. Covers flag design, output formatting, exit codes, and composability with AI-driven workflows. |
| `delegate-first` | Keep the parent conversation clean by forking noisy implementation, validation, and multi-file work to sub-agents. |
| `grill-me` | Socratically stress-test requirements, architecture, edge cases, failure modes, and trade-offs before execution; emits a required risk matrix. |
| [`adversarial-reviewer`](skills/adversarial-reviewer/README.md) | Evidence-based falsification review: validate candidate defects against known controls, separate evidence from severity, and fail closed on unsupported or stale claims. |
| `tdd` | Enforce observed Red → Green → Refactor vertical slices and hand surviving behaviors to the mutation-testing sub-plugin. |
| `karpathy-guidelines` | Behavioral guidelines to reduce common LLM coding mistakes. Helps avoid overcomplication, make surgical changes, surface assumptions, and define verifiable success criteria. |
| `property-based-testing` | Use when implementing serialization/parsing, data transformations, algorithms with mathematical properties, API contracts, or state machines where testing all edge cases is impractical. Describe invariants instead of specific input/output pairs. |
| `verified-implementation` | Require authoritative sources for security-critical, financial, protocol, and production-ready implementation decisions. |

### Documentation & AI Context

| Skill | Description |
|-------|-------------|
| `context7` | Retrieve up-to-date documentation for any software library or framework via the Context7 API. Use instead of relying on potentially outdated training data. |
| `context-file-optimizer` | Audit and rewrite `AGENTS.md`, `CLAUDE.md`, and other AI agent context files to be minimal and effective. Based on research-backed principles: verbose context files hurt performance (−2 to −3%) while minimal, tooling-focused files improve it (+4%). |
| `c4-diagram` | Generate C4 architecture diagrams using standard Mermaid `flowchart` syntax. Covers Context, Container, and Component levels with short labels, companion legend tables, and sequence diagrams for runtime behavior. Never uses the broken C4 Mermaid plugin. |

### Writing

| Skill | Description |
|-------|-------------|
| `writing-about-engineering` | Use when drafting first-person engineering writing — blog posts, short posts/threads, or postmortems. Produces a conversational-but-rigorous, peer-to-peer voice anchored on the Julia Evans / Simon Willison TIL/blog rhythm. |
| `writing-skills-excellence` | Framework for creating, updating, or improving agent skills. Covers structure, frontmatter, when-to-use clauses, and quality principles. |
| `skillopt-sleep-learned` | Locally learned agent preferences and procedures maintained by SkillOpt-Sleep. |

### Automation & Pipelines

| Skill | Description |
|-------|-------------|
| `pas-pipeline` | Author, validate, and run PAS DOT pipelines across Codex, Claude Code, and Gemini. Detects authenticated subscription CLIs and current models, enforces the conditional-label contract, and covers bounded execution and checkpoint resumption. |
| `reck-factory` | Manage the Reck software factory — register repos, run AI tasks in containers, schedule background pipelines, and monitor results via Loki/Grafana. |

### Systems Thinking

| Skill | Description |
|-------|-------------|
| `thinking-in-systems` | Apply Donella Meadows' systems thinking framework to map, diagnose, and redesign any system — organizational, technical, ecological, or policy. Covers stocks/flows, feedback loops, system archetypes, leverage points, and concrete intervention recommendations. Use with `--design` to build a new system, or `--focus map/archetypes/leverage` to run a partial analysis. |
| `emergent-behavior` | Identify system-level behavior arising from component interactions, test whether it is genuinely emergent, seek disconfirming evidence, and recommend measurable interaction-level interventions. |

### Code Review

Each of these is runnable directly via its own slash command, and can also be invoked automatically by the model or by another orchestrating skill/agent (e.g. review-panel) when its checkpoints are relevant to the diff under review.

| Skill | Description |
|-------|-------------|
| `thermo-nuclear` | Zero-mercy structural-simplification doctrine grounded in Cursor's thermo-nuclear-code-quality-review skill. Biased against approval by default; blocks on file-size growth, spaghetti-branching, and missed simplification opportunities that other reviewers would let pass. |
| `google-standard` | Google's published Standard of Code Review. Biased toward approval once a change definitely improves code health, even if imperfect; separates blocking findings from optional `Nit:` findings. |
| `polyglot-idiom` | Per-language idiom checkpoints for Java, C++, C#, Ruby, and PHP, grounded in the Gemini code-quality research. Excludes Python, TypeScript, Go, Rust, and Swift, which have (or will have) dedicated simplifier skills. |
| `concurrency-atomicity` | Four-checkpoint concurrency-correctness review — race conditions, TOCTOU, deadlock/lock-ordering, and transactional atomicity — grounded in fetched CWE reference entries (CWE-362, CWE-367, CWE-833, CWE-667, CWE-662). |

---

## Hooks (5)

| Hook | Event | Description |
|------|-------|-------------|
| `terminal-bell` | `Stop` | Terminal tab indicator when Claude finishes responding. Sends a BEL character for tab/dock notification, a desktop notification with a brief summary, and supports Ghostty/iTerm2 (OSC 9) and WezTerm (OSC 777). |
| `toon-post-hook` | `PostToolUse` | Encodes large tool responses to TOON format (a compact alternative to JSON) before they enter the context window. Reduces token consumption on verbose MCP and built-in tool outputs. No-op if `toon` is not installed. |
| `prefer-modern-tools` | `PreToolUse` | Rewrites legacy CLI commands to faster modern equivalents at runtime: `grep`/`egrep` → `rg`, `cat` → `bat --style=plain --paging=never`, `ls` → `lsd`, `ps aux`/`ps -ef` → `procs`. Safe near-drop-ins only — tools with incompatible flag syntax (`fd`, `dust`, `choose`) are excluded and documented in CLAUDE.md for native use. |
| `data-layer-guard` | `PreToolUse` | Warns and asks for confirmation before an Edit/Write/NotebookEdit touches a data-layer path (migrations, schemas, ORM models — default globs overridable via `.data-guard.json`) without a same-day `DATA-MODEL.md` change-log entry. Interactive/planning-time only: silently no-ops in unattended contexts (`--dangerously-skip-permissions`/`mode:agent`), deferring to the data-steward review seat for unattended enforcement. |
| `post-compaction` | `SessionStart` (`compact`, `clear`) | Restores the active plan and compact recovery context after Claude Code compacts or clears a session. |

> **Hooks only run when this repo is installed as a plugin.** The hook wiring lives in [`hooks/hooks.json`](hooks/hooks.json), which Claude Code loads and expands `${CLAUDE_PLUGIN_ROOT}` from automatically — but only for plugins installed via `/plugin marketplace add` (or `/plugin install`). The root [`.claude/settings.json`](.claude/settings.json) in this repo ships `"hooks": {}` on purpose: it is the config Claude Code reads if you just `git clone` this repo and open it as a plain project, and `${CLAUDE_PLUGIN_ROOT}` has no meaning there.
>
> **Net effect: a plain git-clone checkout has zero hook enforcement active**, including `data-layer-guard` — the guard that's supposed to stop an Edit/Write from silently touching a migration/schema/ORM file without a `DATA-MODEL.md` entry. If you clone this repo directly instead of installing it as a plugin, that protection (and the other three hooks) simply never runs; nothing will warn you that it's missing.
>
> If you want the hooks active without installing the plugin, either:
> - Install via the marketplace (`/plugin marketplace add citadelgrad/scott-cc`) so Claude Code wires `hooks/hooks.json` up for you, or
> - Hand-edit your own `.claude/settings.json` and copy the hook entries from `hooks/hooks.json`, replacing `${CLAUDE_PLUGIN_ROOT}` with the absolute path to your clone (e.g. `/Users/you/scott-cc`).

---

## Templates (3)

Two templates are stored in `templates/`; the `/init` skill uses them to produce three project artifacts.

| Template | Deployed by | Description |
|----------|-------------|-------------|
| `.pre-commit-config.yaml` | `init` skill | Canonical pre-commit hooks: general hygiene (trailing whitespace, file checks), Python (ruff lint+format, ty type check), TypeScript/JS (biome lint+format), security (gitleaks secret scanning). |
| `CLAUDE.md` | `init` skill | Global Claude Code instructions template covering CLI tool preferences, direnv/Makefile/port conventions, uv-only Python, and C4 diagram standards. |
| `AGENTS.md` | `init` skill (symlink) | Global Codex/agent instructions template. Deployed as a symlink pointing to `CLAUDE.md` so both agents share the same instructions. |

---

## Sub-plugins (9)

Install from the marketplace:

```bash
/plugin marketplace add citadelgrad/scott-cc/<name>
```

**Status** reflects real git activity, not a manual label. Derived from each plugin's `git log -- plugins/<name>/`:

| Status | Rule |
|--------|------|
| `stable` | 3+ commits and a commit within the last 60 days |
| `experimental` | Fewer than 3 commits, or 3+ commits with the latest activity 61–89 days ago |
| `unmaintained` | No history or 90+ days since the latest commit |

| Plugin | Status | Description |
|--------|--------|-------------|
| `beads-epic-builder` | `stable` | Plan, build, and swarm beads epics — sequential and parallel execution with CE code review. |
| `browser-automation` | `stable` | Browser testing and validation with E2E test generation and UI validation. |
| `research-tools` | `stable` | Learning guides, tech stack research, and technical writing assistance. |
| `security-suite` | `stable` | Security advisory and vulnerability scanning. |
| `performance-optimization` | `unmaintained` | Performance engineering with bottleneck analysis and profiling. |
| `mutation-testing` | `stable` | Comprehensive mutation testing with zombie test detection and automated refactoring. |
| `review-panel` | `stable` | Context-bounded, checkpointed multi-persona adversarial code and design review panel. |
| `variant-explorer` | `stable` | Parallel blind-builder variant exploration with AC/taste/simplicity judging. |
| `triage` | `stable` | Foundry-resident triage spine: detect → bead → reproduce → fix → gate loop. |

---

### beads-epic-builder

Plan, build, and swarm beads epics — sequential and parallel execution with CE code review.

**Agents (2)**

| Agent | Description |
|-------|-------------|
| `epic-planner` | Plan complete features from initial concept through implementation-ready beads tasks. Orchestrates research, documentation, and task breakdown with approval gates. |
| `feature-builder` | Orchestrate complete feature development from epic to deployment. Manages architecture review, implementation, quality gates, and validation using beads for task tracking. |

**Commands (3)**

| Command | Description |
|---------|-------------|
| `/epic-planner <feature description>` | Plan a feature from concept to implementation-ready beads tasks. Starts the `epic-planner` agent: research → approval → PRD/SPEC → approval → task decomposition, with human approval gates. |
| `/build-feature <epic-id>` | Build a complete feature from a beads epic with sequential architecture review, implementation, and validation. Supports `--resume` for interrupted runs. |
| `/epic-swarm <epic-id>` | Build all tasks in a beads epic using parallel worker agents in isolated git worktrees, with CE code review after. Options: `--max-parallel 3`, `--no-review`, `--dry-run`. |

---

### browser-automation

Browser testing and validation with E2E test generation and UI validation.

**Agents (2)**

| Agent | Description |
|-------|-------------|
| `browser-validator` | Validate UI implementations using browser automation with Playwright MCP tools for real-time verification and test generation. |
| `e2e-runner` | End-to-end testing specialist using browser-use AI automation. Handles authenticated flows with secure credential injection, supports persistent browser profiles for 2FA, and generates Python test scripts. |

**Skills (2)**

| Skill | Description |
|-------|-------------|
| `browser-use` | Automate browser interactions for web testing, form filling, screenshots, and data extraction. |
| `browser-use-e2e` | Generate and run E2E tests using browser-use AI automation. Handles credentials securely via `.env.test` with domain-prefixed variables. |

---

### research-tools

Learning guides, tech stack research, and technical writing assistance.

**Agents (3)**

| Agent | Description |
|-------|-------------|
| `learning-guide` | Teach programming concepts and explain code with focus on understanding through progressive learning and practical examples. |
| `tech-stack-researcher` | Guide technology choices, architecture decisions, and implementation approaches when planning new features or functionality. Invoked proactively during planning discussions before implementation begins. |
| `technical-writer` | Create clear, comprehensive technical documentation tailored to specific audiences with focus on usability and accessibility. |

**Skills (1)**

| Skill | Description |
|-------|-------------|
| `humanizer` | Remove AI writing patterns to make generated text sound more natural and human, based on WikiProject AI Cleanup guidelines. |

---

### security-suite

Security advisory and vulnerability scanning.

**Agents (2)**

| Agent | Description |
|-------|-------------|
| `security-advisor` | Answer security questions, review architecture for vulnerabilities, and provide tailored guidance by searching OWASP cheatsheets. |
| `security-engineer` | Identify security vulnerabilities and ensure compliance with security standards and best practices. |

**Skills (1)**

| Skill | Description |
|-------|-------------|
| `plan-security-review` | Runs a lightweight threat-model checkpoint over a plan/PRD/spec document at the end of planning — trust boundaries, new data flows, authn/authz surface, secrets, third-party deps — producing a CLEAR/TRIGGERED/N/A report and go/no-go. Planning-stage counterpart to review-panel's Security seat, which reviews diffs. |

---

### performance-optimization

Performance engineering with bottleneck analysis and profiling.

**Agents (1)**

| Agent | Description |
|-------|-------------|
| `performance-engineer` | Optimize system performance through measurement-driven analysis and bottleneck elimination. |

---

### mutation-testing

Comprehensive mutation testing with zombie test detection and automated refactoring.

**Agents (5)**

| Agent | Description |
|-------|-------------|
| `test-quality-reviewer` | Orchestrate comprehensive mutation testing workflow for test quality analysis using semantic code mutations and parallel test execution. |
| `test-saboteur` | Create semantic code mutations — applies realistic bugs to verify whether test suites catch them. |
| `test-executor` | Execute test suites against mutated code and collect detailed results for mutation testing analysis. |
| `test-auditor` | Analyze mutation testing results to calculate mutation score and identify zombie tests, redundant tests, and quality issues. |
| `test-refactor-specialist` | Consolidate redundant tests and generate improved test suites based on mutation testing analysis. |

**Skills (1)**

| Skill | Description |
|-------|-------------|
| `mutation-test` | Run comprehensive mutation testing to audit test quality, find zombie tests, and propose refactoring. |

---

### review-panel

Multi-persona adversarial code and design review panel, vendoring and adapting patterns from compound-engineering, clairvoyance, superpowers, ponytail, and mattpocock/skills. Version 1.0 makes the orchestrator context-bounded: detailed stage data stays in workspace artifacts, oversized targets fail before CAST, and continuing rounds resume from one-shot checkpoints in fresh Claude Code sessions. See the [v1.0 context-hardening guide](docs/review-panel-context-hardening.md) and [visual engineering brief](docs/reports/review-panel-context-hardening.html).

**Commands (1)**

| Command | Description |
|---------|-------------|
| `/review-panel [base..head \| branch \| PR] [--mode=agent] [--lite \| --medium \| --auto]` | Run one bounded review target — human-interactive by default, unattended with `--mode=agent`, or narrowed with an explicit/automatic tier. Dirty continuing rounds emit a fresh-process resume command with `--resume` and `--checkpoint-sha256`. |

**Agents (1)**

| Agent | Description |
|-------|-------------|
| `clean-room-alternative` | Generates a design alternative in isolation, without seeing the first design. Used by `design-it-twice` when a first design already exists in conversation. |

**Skills (33)**

*Panel orchestration & review seats*

| Skill | Description |
|-------|-------------|
| `review-panel` | Orchestrates a full multi-reviewer code-review panel: casts reviewer seats from diff content, runs them concurrently, merges/dedupes findings, fixes, and re-reviews to convergence. |
| `adversarial-reviewer` | Red-teams code, PRs, and designs — hunts for bugs, security holes, and hostile/malformed input handling. |
| `design-review` | Orchestrates a diagnostic funnel through complexity, structural, interface, and red-flags checks for overall design quality. |
| `domain-modeling` | Reviews a domain model against 8 type-driven functional-modeling techniques (illegal-states-unrepresentable, parse-don't-validate, etc.). |
| `code-evolution` | Evaluates whether diffs to existing code look designed-in or bolted-on. |
| `red-flags` | Scans code against 17 design smells and produces a structured diagnostic report. |
| `strategic-mindset` | Assesses whether code reflects strategic or tactical design investment. |
| `ponytail-review` | Reviews diffs exclusively for over-engineering — what to delete, replace with stdlib, or simplify. |
| `ponytail-audit` | Whole-repo version of `ponytail-review` — a ranked list of what to delete or simplify. |
| `data-steward` | Reviews migration/ORM/schema diffs against `DATA-MODEL.md` invariants and a 7-item migration-safety checklist. |
| `taste-review` | Reviews diffs against `TASTE.md`'s Preferences/Weightings/Anti-preferences, mapping severity from declared strength. |
| `mental-models-adversarial` | Pressure-tests the reasoning behind a change — assumptions, incentives, second-order consequences — via fs.blog-inspired mental models. |
| `mental-models-simplifier` | Questions whether this is even the right problem/approach, conceptually, via fs.blog-inspired mental models. |
| `mental-models-systems` | Evaluates dynamic runtime behavior — feedback loops, bottlenecks, emergence, scale — via fs.blog-inspired mental models. |
| `mental-models-economics` | Frames a change as a resource-allocation decision — tech debt, build-vs-buy, vendor lock-in — via fs.blog-inspired mental models. |

*Design quality lenses*

| Skill | Description |
|-------|-------------|
| `abstraction-quality` | Evaluates whether abstractions are genuinely useful or structurally shallow. |
| `complexity-recognition` | Diagnoses what makes code complex and why, via a three-symptom two-root-cause framework. |
| `comments-docs` | Reviews comment quality and documentation practices. |
| `deep-modules` | Measures module depth — whether the interface is simple relative to its implementation. |
| `error-design` | Reviews error-handling strategy against the "define errors out of existence" principle. |
| `general-vs-special` | Evaluates whether interfaces are appropriately general-purpose. |
| `information-hiding` | Checks for information leakage across module boundaries. |
| `module-boundaries` | Evaluates where module boundaries are drawn and whether modules should merge or split. |
| `naming-obviousness` | Reviews naming quality and code obviousness. |
| `pull-complexity-down` | Checks whether complexity is pushed to callers or absorbed by implementations. |
| `diagnose` | Routes a vague symptom to the most relevant design-quality lens via a decision tree. |

*Grilling / interview*

| Skill | Description |
|-------|-------------|
| `grill-my-taste` | Elicits personal taste via forced-choice interviews, distilling picks into `TASTE.md` Preferences/Weightings/Anti-preferences. |
| `grill-the-schema` | Interviews to build `DATA-MODEL.md` — entities, invariants, lifecycle, volume/access patterns, Agent boundaries. |
| `grill-with-docs` | Challenges a plan against the existing domain model, updating `CONTEXT.md`/ADRs inline. |

*Architecture & planning*

| Skill | Description |
|-------|-------------|
| `adr-skill` | Create and maintain Architecture Decision Records with Socratic questioning and an agent-readiness checklist. |
| `design-it-twice` | Generates and compares at least two independent design alternatives before committing. |
| `improve-codebase-architecture` | Finds deepening/refactoring opportunities informed by `CONTEXT.md` and ADRs. |

*Development workflow*

| Skill | Description |
|-------|-------------|
| `tdd` | Test-driven development with the red-green-refactor loop. |

---

### variant-explorer

Parallel blind-builder variant exploration: spawns N isolated implementations against a spec and acceptance criteria, judges them against AC conformance, `TASTE.md`, and simplicity, and produces a ranked shortlist.

**Commands (1)**

| Command | Description |
|---------|-------------|
| `/explore-variants [spec] [--n N] [--ac <path>]` | Spawn N blind builders in isolated worktrees against a spec + acceptance criteria, then judge the results and produce a ranked shortlist. |

**Agents (2)**

| Agent | Description |
|-------|-------------|
| `blind-builder` | Builds one complete, independent implementation of a spec inside an isolated worktree, without seeing any sibling variant. |
| `variant-judge` | Scores every surviving variant along one judging axis (AC-conformance, taste, or simplicity) and returns a scorecard per variant. Never edits variant code. |

**Skills (1)**

| Skill | Description |
|-------|-------------|
| `explore-variants` | Orchestrates the panel: gathers input, validates N (refuses N=1, clamps N>6), spawns blind builders, collects results with explicit failure reporting, runs the judge panel, and hands the human a ranked shortlist. |

---

### triage

Foundry-resident triage spine: detect → bead → reproduce → fix → gate loop turning detector findings into beads, E2E-reproduced fixes, and `review-panel --mode=agent` gated PRs.

**Skills (3)**

| Skill | Description |
|-------|-------------|
| `triage-spine` | Consumes normalized triage items from any registered detector and runs the loop — intake → reproduce → diagnose → fix → gate — filing a bead per item, reproducing the issue E2E before any fix, producing a fix diff via `pas-pipeline`, and gating it through `review-panel --mode=agent`. |
| `detectors/lib-upgrades` | Scans a project's dependency manifest/lockfile(s) for outdated or CVE-flagged libraries and emits normalized triage items. |
| `detectors/prod-errors` | Consumes a log/Sentry-shaped production error source and emits one normalized triage item per distinct error, stack trace carried verbatim in evidence. |

Two of five detector slots (`lib-upgrades`, `prod-errors`) are implemented in v1; three (`system-upgrades`, `iac-drift`, `security-advisory-sweeps`) are registered but stubbed.

---

## Setup Architecture

This plugin is one layer of a three-layer setup system:

| Layer | What | How |
|-------|------|-----|
| 1 — Machine | Ansible `ai-tools` role | `./bootstrap.sh` in macOS-config — clones this repo, installs tools, deploys security configs |
| 2 — Portable skills | This repo (`scott-cc`) | `npx skills add citadelgrad/scott-cc` — select skills and Codex, Hermes Agent, Claude Code, or other targets |
| 3 — Claude plugin | This repo (`scott-cc`) | `/plugin marketplace add citadelgrad/scott-cc` in Claude Code |
| 4 — Project | `/init` skill | Run per-project to scaffold `CLAUDE.md`, `AGENTS.md`, `.envrc`, `Makefile`, pre-commit hooks |

Full bootstrap instructions and Ansible configuration: **[citadelgrad/macOS-config](https://github.com/citadelgrad/macOS-config)**

See `docs/setup-architecture.md` for context and init sequence diagrams.

---

## Code Quality Standards

All code follows these principles (enforced by simplifier skills):

- **DRY** — Remove duplicate code
- **KISS** — Straightforward over clever
- **Thin Handlers** — Business logic in services
- **No Hardcoded Values** — Use config/env
- **No Silent Failures** — Fail fast, specific exceptions
- **Function Size** — ~20 lines max
- **No Premature Abstraction** — Wait for 3+ patterns

## Best For

- Full-stack engineers
- Next.js / React developers
- Python (FastAPI, Django) developers
- TypeScript projects
- Teams using beads for task tracking

## Installation

### Portable Skills for Codex, Hermes Agent, and Other Agents

```bash
npx skills add citadelgrad/scott-cc
```

For a scripted global install into both Codex and Hermes Agent:

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

See [docs/skills-cli.md](docs/skills-cli.md) for the complete guide.

### Complete Claude Code Plugin

```bash
/plugin marketplace add citadelgrad/scott-cc
```

### Update Existing Installation

```bash
/plugin marketplace update scott-cc
```

### From Local Clone (for development)

```bash
git clone https://github.com/citadelgrad/scott-cc.git
/plugin install /path/to/scott-cc
```

## Requirements

- Claude Code CLI
- Works with any project (optimized for Next.js, FastAPI, TypeScript)
- Beads plugin recommended for epic workflows

## License

MIT — Use freely in your projects
