# Execution Protocol

## Agent Registry

The registry uses a manifest + shards structure to keep context lean.

**Always start here:**
```
agents/registry/manifest.json
```

Read the manifest to identify which shards are relevant to the task.
Load only those shards. Never load all shards at once.

**Always load:** `meta` shard (orchestrator, agent-organizer, context-manager)
**Load for complex/phased tasks:** `pipeline` shard (planner, gate agents)
**Load by task domain:** see manifest `load_when` fields per shard

---

## Pipelines are per-session and namespaced

Pipeline state lives under `.claude/pipelines/<pipeline-id>/`, keyed by session
id, so one project folder can host one OR several concurrent pipelines uniformly.
Sessions ALWAYS run in the main project folder (no git worktree, no directory
switching). The `<pipeline-id>` **defaults to the session id**, so a single
pipeline needs no special handling — it is just the N=1 case.

- **Per-pipeline state** — `.claude/pipelines/<pipeline-id>/`:
  - `pipeline.json` — phase plan + gate-agent lists per phase (written by planner)
  - `phase-state.json` — the gate ledger: current index, completed phases, gate
    results (written by hooks ONLY)
  - `gate-artifacts/<phase-id>/<agent>.json` — machine-checked verdict evidence
- **Binding** — `.claude/bindings/<session-id>` — contents are the `<pipeline-id>`;
  present ONLY when the id differs from the session id (a named pipeline, or a
  fresh-session reattach).
- Hooks resolve the base automatically from the payload `session_id` (stable
  across compaction and `--resume`). You never edit the ledger or bindings by
  hand — the pre-tool-use hook forbids it.

Session id: read from the newest transcript basename
(`ls -t ~/.claude/projects/<encoded-project-dir>/*.jsonl | head -1`, minus
`.jsonl`) or your scratchpad path.

---

## Phased Execution Protocol

### When to use phases

A task is multi-phase if it has separable stages where later work depends
on earlier work being complete and verified. If it would naturally be
described as "first X, then Y, then Z" where each step gates the next,
use phases. Single-step tasks do not need phases.

### Mandatory sequence
1. **Load the pipeline shard** from the registry.
2. **Create the pipeline namespace:** read your session id, then
   `~/.claude/hooks/pipeline-ctl.sh init <session-id> [pipeline-id]`
   (omit the id for a single pipeline — it defaults to the session id; pass an
   explicit id only to run several at once with meaningful names).
3. **Invoke the `planner` agent** with the full task description, told to write
   `pipeline.json` **into** `.claude/pipelines/<pipeline-id>/`. Wait for
   confirmation that it has been written.
4. **Read** `.claude/pipelines/<pipeline-id>/pipeline.json` to understand all
   phases before starting work.
5. **Execute phases in index order** using agents from relevant registry shards.
6. **After each phase**, invoke gate agents one at a time in the order listed in
   that phase's `pipeline.json` entry. Each gate agent's FINAL action is:
   `~/.claude/hooks/emit-gate-verdict.sh <phase-id> <agent> PASS|FAIL [gtest-xml] [target-test] --pipeline <pipeline-id>`
7. **Never advance** to the next phase until all gate agents have passed
   (artifact-verified — see the Gate Reliability Protocol).
8. **On gate failure**: re-invoke the implementing agent to fix the issue, re-run
   the failed gate — do not skip or override.
9. **After all gates pass**, run `/g` to commit the phase's verified work; the
   phase advances automatically after `/g` completes.

### Gate agents (all in pipeline shard)

| Agent | Purpose | Include when |
|---|---|---|
| `karen` | Reality-checks actual vs claimed completion — may prompt user | Always — listed first |
| `validator` | Verifies acceptance criteria | Always — listed second |
| `test-runner` | Runs test suite | Phase produces testable code |
| `security-audit` | Scans for secrets/vulns | Phase touches auth, secrets, infra |
| `perf-benchmarks` | Runs benchmarks | Phase affects performance-critical paths |

### Gate Reliability Protocol (verdicts from artifacts; survives the post-compact hook bug)

Read this every session — it is durable where mid-conversation learning is not.

- **Verdicts come from artifacts, never prose.** A gate passes only when a
  machine-checked artifact exists at
  `.claude/pipelines/<pipeline-id>/gate-artifacts/<phase-id>/<agent>.json` =
  `{"verdict":"PASS"}`, plus (for `test-runner`/`perf-benchmarks`) a green
  `gtest-results.xml` in that same dir. Emit them with `emit-gate-verdict.sh`
  (step 6) so they are byte-correct and correctly located — nothing is
  interpreted from free prose.
- **After any /compact or session resume, before acting on a pipeline:**
  1. Re-ground from ground truth, not conversational memory: read
     `.claude/pipelines/<pipeline-id>/pipeline.json` and the `gate-artifacts/`
     evidence (and `pipeline-status.snapshot` if present).
  2. Run `~/.claude/hooks/check-hooks-alive.sh <session-id>`. If it prints
     **DEAD**, the Stop/SubagentStop recording hooks are not firing (the compact
     bug). **HARD STOP:** do not commit, do not advance, do not treat any gate as
     passed; tell the user the hooks appear dead and recommend restarting the
     session, then stop. (You cannot restart the session yourself — it is the
     user's action.)
- `hook-liveness-guard.sh` (PreToolUse) enforces the same rule mechanically by
  blocking `git commit` when a compaction occurred with no heartbeat since. Every
  path fails closed: the worst case is a blocked commit, never a forged or stale pass.

### State files (per pipeline; read by hooks automatically)

- `.claude/pipelines/<pipeline-id>/pipeline.json` — phase plan + gate lists (planner).
- `.claude/pipelines/<pipeline-id>/phase-state.json` — the gate ledger (hooks only).
- `.claude/pipelines/<pipeline-id>/gate-artifacts/` — verdict evidence.

Do not modify the ledger or bindings manually during execution — the hooks own them.

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
5. Do not advance the gate ledger via Bash to bypass the hook.
6. Do not fabricate gate results by writing `true` into the ledger
   without running the agent (verdicts come from artifacts only).
7. The pre-tool-use hook enforces mechanically: it resolves the current
   session's pipeline ledger, then blocks Bash/Write/Edit once a gate is
   invoked until all pass; blocks `git commit` if gates pending; hard-blocks
   `current_gate_results` modification while gates are in progress; prompts user
   on `phases_complete`/`current_phase_index` manipulation.

---

## Multi-Pipeline quick reference

- **Start any pipeline:** `~/.claude/hooks/pipeline-ctl.sh init <session-id> [pipeline-id]`.
- **Resume / after compaction:** nothing — the session id (and its binding) survive.
- **Continue in a genuinely fresh session (not --resume):**
  `~/.claude/hooks/pipeline-ctl.sh reattach <new-session-id> <pipeline-id>`,
  recovering the id from the restored conversation.
- **Inspect:** `pipeline-ctl.sh status <session-id>` | `list` | `resolve <session-id>`.

*(This section is self-contained — the multi-pipeline mechanism is fully described
above; no separate protocol file is needed. Migration note: a pre-existing legacy
pipeline at singular `.claude/pipeline.json` is no longer auto-resolved — move it
into `.claude/pipelines/<session-id>/` or complete it before deploying.)*

## Commands

- `/a <task>` — reads manifest, auto-detects whether phases are needed
- `/p <task>` — explicitly invokes planner first, then executes

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
- STOP=FAIL! When there is authorized work with a clear or already-approved next step, DO IT. NEVER pause to report progress and wait, ask permission for the obvious/authorized next step, offer a menu when one option is clearly correct, or "check in" at a milestone. Drive multi-phase tasks and pipelines through ALL phases and gates to completion in ONE continuous push. A completed phase/gate/milestone is NOT a reason to stop — roll straight into the next. Only stop for a GENUINE user-decision blocker: a real fork with material trade-offs, an unauthorized destructive/irreversible action, or a hard technical dead-end. Report by DOING and summarizing after, never by stopping before. Commit via `git -C` yourself; never hand a commit to me.
- If I appear to be running on a Windows system that does not have WSL then be sure check for other tools like Python, the Git "bash toolbox" and PowerShell, taking this into account when handling file edits, searches etc.
- Always add UTF-8 unicode support explicitly to python scripts.
- Always use SSH keys when available!
- Always use available agents!
- Whenever **web** UI elements are modified or added, make them "Selenium friendly" for ease of testing. For **native desktop** UI elements, make them automation-friendly for pywinauto/WinAppDriver/FlaUI (stable control IDs, accessible names, AutomationId properties).
