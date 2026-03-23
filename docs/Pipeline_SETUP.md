# Claude Phased Pipeline v2 — Setup Guide

## What's in this package

A complete toolchain for Claude Code that provides:

- **Manifest-sharded registry** — your 109 agents split by their existing
  `tier` field into 10 shards + 1 new pipeline shard (114 total). Claude
  reads the manifest first and loads only relevant shards, keeping context
  well under limits regardless of registry size.
- **Automatic phase detection** — `UserPromptSubmit` hook intercepts
  multi-step tasks and enforces planner invocation.
- **Dynamic pipeline planning** — `planner` agent writes `pipeline.json`
  for any number of phases; all hooks are data-driven from that file.
- **Sequential gate enforcement** — `phase-gate.sh` emits one gate at a
  time in strict array order, supporting gates that pause to prompt.
- **Session resilience** — `session-resume.sh` reinjects pipeline context
  after restarts.
- **Internet gating** — `pre-tool-use.sh` forces approval on all network
  operations in `bypassPermissions` mode.

---

## Directory layout

```
.claude/
  agents/
    pipeline/               ← 5 new pipeline agents
      planner.md
      validator.md
      test-runner.md
      security-audit.md
      perf-benchmarks.md
  commands/
    run.md                  ← manifest-aware, auto-detects phases
    plan-and-run.md         ← explicit phased entry point
  hooks/
    pre-tool-use.sh         ← internet gating
    require-pipeline.sh     ← enforces planner before phase work
    validate-pipeline.sh    ← validates pipeline.json at phase 0
    phase-gate.sh           ← advances phases, one gate at a time
    session-resume.sh       ← reinjects state on restart
  settings.json             ← wires all hooks

registry/
  manifest.json             ← always-loaded index; maps domains to shards
  shards/
    pipeline.json           ← planner + 4 gate agents (new)
    architecture.json       ← 8 agents (incl. infrastructure)
    business.json           ← 8 agents
    data-ai.json            ← 10 agents
    developer-experience.json ← 7 agents
    development.json        ← 28 agents
    meta.json               ← 3 agents
    research.json           ← 4 agents
    security.json           ← 11 agents
    specialized-domains.json ← 8 agents
    testing.json            ← 22 agents

CLAUDE.md                   ← execution protocol, loaded every session
docs/
  SETUP.md                  ← this file
```

---

## Installation

### 1. Make hooks executable

```bash
chmod +x .claude/hooks/*.sh
```

### 2. Place agent files

Pipeline agents go wherever your other agents live. If your existing agents
are at `~/.claude/agents/`, copy:

```bash
cp -r .claude/agents/pipeline ~/.claude/agents/
```

Update `file_path` values in `registry/shards/pipeline.json` to match
your actual path if different from `../pipeline/`.

### 3. Replace your existing capabilities.json

Your original `capabilities.json` is now split into shards. Replace it:

```bash
# Back up original
cp ~/.claude/agents/registry/capabilities.json \
   ~/.claude/agents/registry/capabilities.json.bak

# Copy new structure
cp registry/manifest.json ~/.claude/agents/registry/
cp -r registry/shards ~/.claude/agents/registry/
```

### 4. Merge settings.json

If you already have `.claude/settings.json`, merge the `hooks` block.
If you don't, copy directly:

```bash
cp .claude/settings.json .claude/settings.json
```

### 5. Add commands

```bash
cp .claude/commands/*.md ~/.claude/commands/
# or keep project-local in .claude/commands/
```

### 6. Merge CLAUDE.md

If you already have a `CLAUDE.md`, append the contents of the included
file. If not, copy it:

```bash
cp CLAUDE.md CLAUDE.md
```

### 7. Update your run command

If you have an existing command that references `capabilities.json` directly,
update it to read `registry/manifest.json` first. The included `run.md`
shows the pattern.

---

## Shard sizes (for reference)

| Shard | Agents | Approx size |
|---|---|---|
| development | 28 | ~26KB |
| testing | 22 | ~28KB |
| security | 11 | ~12KB |
| data-ai | 10 | ~9KB |
| architecture | 8 | ~10KB |
| business | 8 | ~7KB |
| specialized-domains | 8 | ~7KB |
| developer-experience | 7 | ~7KB |
| pipeline | 5 | ~4KB |
| research | 4 | ~3KB |
| meta | 3 | ~3KB |
| **manifest only** | — | **~3KB** |

Claude now loads 3KB (manifest) + 2–3 relevant shards (~15–40KB) instead
of the full 117KB on every task.

---

## Adding new agents to the registry

1. Create the agent `.md` file in the appropriate directory
2. Add an entry to the correct shard in `registry/shards/<tier>.json`
   using the same keyed format as existing entries
3. If it's a new tier, create a new shard file and add it to `manifest.json`

## Adding gate agents to the pipeline

1. Create `.claude/agents/pipeline/<agent-name>.md` following the pattern
   of the included gate agents (read phase state → do work → write result
   to `current_gate_results.<name>` → report PASS/FAIL)
2. Add to `registry/shards/pipeline.json` with `"gate_role": true`
3. Reference by name in any phase's `gate_agents` array in `pipeline.json`
4. Add to planner's gate agent selection logic in `planner.md`

The `phase-gate.sh` hook requires no changes — it reads agent names
dynamically from `pipeline.json`.
