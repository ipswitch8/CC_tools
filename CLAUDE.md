# Execution Protocol

## Agent Registry

The registry uses a manifest + shards structure to keep context lean.

**Always start here:**
```
registry/manifest.json
```

Read the manifest to identify which shards are relevant to the task.
Load only those shards. Never load all shards at once.

**Always load:** `meta` shard (orchestrator, agent-organizer, context-manager)
**Load for complex/phased tasks:** `pipeline` shard (planner, gate agents)
**Load by task domain:** see manifest `load_when` fields per shard

---

## Phased Execution Protocol

### When to use phases

A task is multi-phase if it has separable stages where later work depends
on earlier work being complete and verified. If it would naturally be
described as "first X, then Y, then Z" where each step gates the next,
use phases. Single-step tasks do not need phases.

### Mandatory sequence

1. **Load the pipeline shard** from the registry
2. **Invoke the `planner` agent** with the full task description
   Wait for confirmation that `pipeline.json` has been written
3. **Read `pipeline.json`** to understand all phases before starting work
4. **Execute phases in index order** using agents from relevant registry shards
5. **After each phase**, invoke gate agents one at a time in the order listed
   in that phase's `pipeline.json` entry
6. **Never advance** to the next phase until all gate agents have passed
7. **On gate failure**: re-invoke the implementing agent to fix the issue,
   re-run the failed gate — do not skip or override

### Gate agents (all in pipeline shard)

| Agent | Purpose | Include when |
|---|---|---|
| `validator` | Verifies acceptance criteria | Always — listed first |
| `test-runner` | Runs test suite | Phase produces testable code |
| `security-audit` | Scans for secrets/vulns | Phase touches auth, secrets, infra |
| `perf-benchmarks` | Runs benchmarks | Phase affects performance-critical paths |

### State files (written by planner, read by hooks)

- `.claude/pipeline.json` — full phase plan, gate agent lists per phase
- `.claude/phase-state.json` — current index, completed phases, gate results

Hooks read these automatically. Do not modify them manually during execution.

---

## Commands

- `/run <task>` — reads manifest, auto-detects whether phases are needed
- `/plan-and-run <task>` — explicitly invokes planner first, then executes
