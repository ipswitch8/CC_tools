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
5. **After each phase**, invoke gate agents one at a time in the order listed in that phase's `pipeline.json` entry
6. **Never advance** to the next phase until all gate agents have passed
7. **On gate failure**: re-invoke the implementing agent to fix the issue, re-run the failed gate — do not skip or override
8. **After all gates pass**, run `/g` to commit the phase's verified work
9. **Phase advances** automatically after `/g` completes

### Gate agents (all in pipeline shard)

| Agent | Purpose | Include when |
|---|---|---|
| `karen` | Reality-checks actual vs claimed completion — may prompt user | Always — listed first |
| `validator` | Verifies acceptance criteria | Always — listed second |
| `test-runner` | Runs test suite | Phase produces testable code |
| `security-audit` | Scans for secrets/vulns | Phase touches auth, secrets, infra |
| `perf-benchmarks` | Runs benchmarks | Phase affects performance-critical paths |

### State files (written by planner, read by hooks)

- `.claude/pipeline.json` — full phase plan, gate agent lists per phase
- `.claude/phase-state.json` — current index, completed phases, gate results

Hooks read these automatically. Do not modify them manually during execution.

### Gate Discipline

Gate agents are not optional overhead. They are the mechanism that
converts work from "probably fine" to "verified correct."

**Evidence:** In every project where gates have been used, every
gate review has caught real issues (false-green oracles, fabricated
results, inflated completion claims, premature scope closures).
Every gate skip has caused rework costing 2-5x the skipped gate
cycle time. Success rate of skipping gates: **0%**.

**Rules (ordered by likelihood of triggering the anti-pattern):**

1. Gates are mandatory between phases. No exceptions.
2. Context pressure is a signal to **hand back cleanly**, not skip
   gates. A partial phase with honest gates is worth more than a
   complete phase with fabricated ones. This is the most important
   rule — context pressure is the root cause of every documented
   bypass.
3. When a gate agent returns FAIL, the findings are the **next
   work items** — not obstacles to argue away or reclassify.
4. Do not merge phases to reduce the number of gate cycles.
5. Do not advance phase-state.json via Bash to bypass the hook.
6. Do not fabricate gate results by writing `true` into
   phase-state.json without running the agent.
7. The pre-tool-use hook enforces mechanically: blocks
   Bash/Write/Edit once a gate is invoked until all pass; blocks
   `git commit` if gates pending; hard-blocks `current_gate_results`
   modification while gates are in progress; prompts user on
   `phases_complete`/`current_phase_index` manipulation.

---

## Commands

- `/run <task>` — reads manifest, auto-detects whether phases are needed
- `/plan-and-run <task>` — explicitly invokes planner first, then executes

# Project Memory System

**Last Updated:** 2025-10-12
**Version:** 6.3 (Segmented Memory)

> This file serves as the main index for the segmented memory system. Detailed knowledge is organized in topic-specific files under `memory/`.

---

## 📋 Memory Structure

### Architecture Decisions
**Location:** `memory/adr/`

Recent ADRs:
- [001-use-segmented-memory-for-project-tracking](memory/adr/001-use-segmented-memory-for-project-tracking.md)

**Total ADRs:** 1

### Patterns & Conventions
**Location:** `memory/patterns/`

- [architecture-patterns](memory/patterns/architecture-patterns.md)
- [coding-patterns](memory/patterns/coding-patterns.md)

### Services & Components
**Location:** `memory/services/`

- [memory-agent-v63](memory/services/memory-agent-v63.md)
- [pattern-library-v63](memory/services/pattern-library-v63.md)
- [smart-validation-module-v63](memory/services/smart-validation-module-v63.md)

### Agent Coordination
**Location:** `memory/agent-coordination/`

- [Successful Workflows](memory/agent-coordination/successful-workflows.md)

### Technical Debt
**Location:** `memory/technical-debt/`

- [Known Issues](memory/technical-debt/known-issues.md)

---

## 🎯 Quick Reference

### For Enterprise SaaS Projects

This segmented structure is optimized for:
- Distributed architecture documentation
- Microservices management
- Team collaboration via Git
- Architectural decision tracking
- Pattern standardization

### Memory Categories

1. **Architecture** (`memory/architecture/`) - High-level system design
2. **Services** (`memory/services/`) - Service-specific documentation
3. **ADR** (`memory/adr/`) - Architecture Decision Records
4. **Patterns** (`memory/patterns/`) - Coding conventions and patterns
5. **Agent Coordination** (`memory/agent-coordination/`) - Successful workflows
6. **Technical Debt** (`memory/technical-debt/`) - Issues and improvements

---

## 🔮 Usage

### Recording New Information

Use the Memory Agent API:

```python
from memory_agent import MemoryAgent

agent = MemoryAgent()

# Record architecture decision
agent.record_architecture_decision(
    title="Use Event Sourcing for Order Service",
    context="Need audit trail and event replay",
    decision="Implement event sourcing pattern",
    consequences="Increased complexity, better auditability"
)

# Record successful workflow
agent.record_agent_workflow(
    task_description="Implement payment service",
    agents_used=["backend-architect", "backend-developer", "api-specialist"],
    outcome="Payment service deployed successfully",
    duration="2 hours"
)

# Document a service
agent.record_service_documentation(
    service_name="Payment Service",
    description="Handles payment processing via Stripe",
    tech_stack=["Python", "FastAPI", "Stripe SDK"],
    endpoints=[
        {"method": "POST", "path": "/api/payments", "description": "Process payment"}
    ]
)
```

---

*This index is automatically updated by the Memory Agent.*
- For **web apps only**: use Selenium to check UI for errors or other problems. Selenium does NOT apply to native desktop apps — for native Windows apps use PyAutoGUI, pywinauto, WinAppDriver, or FlaUI as appropriate.
- On my responses:- I communicate in a calm, understated way.
    - I have a casual, conversational communication style.
    - I value authenticity over excessive agreeableness.
    - I express well-supported answers.
    - I offer polite corrections and apply reasoned skepticism when needed.
- SIMPLIFY=FAIL!
- SKIP=FAIL!
- NEVER SIMPLIFY OR SKIP A TEST - JUST FIX THE PROBLEM!
- This is a Windows system and does not have WSL, though it does have other tools like Python, the Git "bash toolbox" and PowerShell.  Take this into account when handling file edits, searches etc.
- Always add UTF-8 unicode support explicitly to python scripts.
- Always use SSH keys when available!
- Always use available agents!
- Whenever **web** UI elements are modified or added, make them "Selenium friendly" for ease of testing. For **native desktop** UI elements, make them automation-friendly for pywinauto/WinAppDriver/FlaUI (stable control IDs, accessible names, AutomationId properties).