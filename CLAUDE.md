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
The `<pipeline-id>` **defaults to the session id**, so a single pipeline needs no
special handling — it is just the N=1 case.

Sessions run in the main project folder by default (no git worktree). **Exception
— the working directory is load-bearing for three hook behaviours**, so change it
deliberately when they apply:

  1. A gate agent must emit its verdict from the pipeline root (see step 6).
  2. The shell must be inside the target git repo when the final gate's Stop
     fires (see step 9).
  3. The gating hooks resolve `.claude/` relative to the shell's cwd, so from
     outside the pipeline root they silently abstain. That is a fail-open
     weakness, never a licence to commit from there.

- **Per-pipeline state** — `.claude/pipelines/<pipeline-id>/`:
  - `pipeline.json` — phase plan + gate-agent lists per phase (written by planner)
  - the gate ledger — current index, completed phases, gate results (written by
    hooks ONLY). It is the JSON in that directory which is *not* `pipeline.json`;
    locate it by exclusion. Naming it literally in a command or a file is
    hard-blocked by the pre-tool-use guard, so refer to it as "the gate ledger".
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
   phases before starting work. **Check its acceptance criteria are satisfiable
   from the WORKING TREE** — a criterion requiring a merged PR, a merge SHA or
   deployed state makes the phase unsatisfiable, because the commit that produces
   those is blocked until the gate passes. Send it back to the planner if so.
5. **Execute phases in index order** using agents from relevant registry shards.
6. **After each phase**, invoke gate agents ONE PER TURN in the order listed in
   that phase's `pipeline.json` entry. Each gate agent's FINAL action is:
   ```
   cd <pipeline-root> && ~/.claude/hooks/emit-gate-verdict.sh \
     <phase-id> <agent> PASS|FAIL [gtest-xml] [target-test] --pipeline <pipeline-id>
   ```
   The `cd` is mandatory: the emitter writes a RELATIVE path, so a gate whose
   shell sits elsewhere files a real verdict into a shadow tree nothing reads.
   Require the gate to READ the artifact back and confirm where it landed — do
   not accept "I wrote it" on trust.
7. **Never advance** to the next phase until all gate agents have passed
   (artifact-verified — see the Gate Reliability Protocol).
8. **On gate failure**: re-invoke the implementing agent to fix the issue, re-run
   the failed gate — do not skip or override.
9. **Commit the verified work yourself — never hand a commit back to the user.**

   **Component-repo commits are sanctioned at any time.** The bare-commit guard
   exists to stop an accidental commit of the PIPELINE repo before its gates
   pass. It does not govern the dependency repos a phase produces PRs against
   (infra / rmm-server / edr-platform / …), which carry their own governance:
   GitHub CI, branch protection, signed commits. Use
   `git -C <clone> commit -F <msgfile>` for those, plus `git add/push` and
   `gh pr create/merge`, and do it as part of the normal gate sequence. That is
   the documented pattern, **not** a bypass — do not relabel it as one and stop
   using it. Handing the user a git block to run is the mistake, and it has
   recurred (PRs #554, #555, and again during the 2026-07 security audit).

   The only thing `git -C` must never be pointed at is the pipeline repo itself
   while that repo's own gates are pending. That is the case the guard is for.

   Once every gate passes the guard stops firing anyway — it blocks only while
   some gate is *pending* — so a plain `git commit` also works then. `/g` is
   user-invocable only and is a convenience, never the required route.
   - `advance_loop` runs `git rev-parse HEAD` **in the hook's cwd**. If that
     resolves, the pipeline PARKS and the commit window opens. If it is empty —
     which it is whenever the pipeline root is a workspace holding several repos
     rather than a repo itself — the phase advances IMMEDIATELY, the window never
     opens, and that phase's uncommitted work is stranded: the next phase's gates
     are pending, so commits are blocked again.

   **Therefore: put the shell inside the repo you need to commit BEFORE the final
   gate's Stop fires.** Then commit, push, open the PR. The next Stop sees a new
   HEAD and advances. Determine which case you are in with
   `git -C <pipeline-root> rev-parse HEAD`; do not assume.

### Gate agents (all in pipeline shard)

| Agent | Purpose | Include when |
|---|---|---|
| `karen` | Reality-checks actual vs claimed completion — may prompt user | Always — listed first |
| `validator` | Verifies acceptance criteria | Always — listed second |
| `test-runner` | Runs test suite | Phase produces testable code |
| `security-audit` | Scans for secrets/vulns | Phase touches auth, secrets, infra |
| `perf-benchmarks` | Runs benchmarks | Phase affects performance-critical paths |

Run the evidence gate (`test-runner`/`perf-benchmarks`) LAST: the judgment gates
get no shell unlock, so run them while remediation mode still permits tools.

### Gate Reliability Protocol (verdicts from artifacts; survives the post-compact hook bug)

Read this every session — it is durable where mid-conversation learning is not.
The `inject-gate-rules.sh` UserPromptSubmit hook re-injects the essentials every
turn, so they survive compaction; this section is the reasoning behind them.

- **Verdicts come from artifacts, never prose.** `phase-gate.sh` states it in its
  own header: it never reads the agent's last message. A gate passes only when a
  machine-checked artifact exists at
  `.claude/pipelines/<pipeline-id>/gate-artifacts/<phase-id>/<agent>.json` =
  `{"verdict":"PASS"}`. Emit with `emit-gate-verdict.sh` (step 6) so it is
  byte-correct and correctly located.
- **An evidence gate taxes the judgment gates too.** If the phase's `gate_agents`
  include `test-runner` or `perf-benchmarks`, then `karen`, `validator` and
  `security-audit` are ALSO conditional on a green `gtest-results.xml` in that
  same directory. Four PASS artifacts with no XML clear nothing. Produce it from
  a real run (`--junitxml=…`) and pass the path as the 4th argument.
- **If the ledger is not updating, check cwd BEFORE suspecting the compact bug.**
  `phase-gate.sh` also resolves its base relative to the hook's cwd, so a Stop
  that fires from outside the pipeline root records nothing. A fresh heartbeat
  with a stale ledger means wrong directory, not dead hooks.
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
- **A memory describing tool MECHANISM is a hypothesis.** The hooks in
  `~/.claude/hooks` are the truth and are cheap to read. A confident memory once
  described a prose-parsing verdict system that had already been replaced, and
  cost a day. Read the hook before building a plan on how it behaves.

### State files (per pipeline; read by hooks automatically)

- `.claude/pipelines/<pipeline-id>/pipeline.json` — phase plan + gate lists (planner).
- `.claude/pipelines/<pipeline-id>/` — the gate ledger (hooks only; see above for
  why it is not named here).
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
7. Do not invoke a gate agent OUTSIDE the gate cycle. An ad-hoc gate run
   records a verdict against the current phase and can put the ledger into
   remediation. Use a non-gate agent for ad-hoc review.
8. The pre-tool-use hook enforces mechanically: it resolves the current
   session's pipeline ledger, then blocks Bash/Write/Edit once a gate is
   invoked until all pass; blocks `git commit` while any gate is pending;
   hard-blocks `current_gate_results` modification while gates are in progress;
   prompts user on `phases_complete`/`current_phase_index` manipulation.

---

## Multi-Pipeline quick reference

- **Start any pipeline:** `~/.claude/hooks/pipeline-ctl.sh init <session-id> [pipeline-id]`.
- **Resume / after compaction:** nothing — the session id (and its binding) survive.
- **Continue in a genuinely fresh session (not --resume):**
  `~/.claude/hooks/pipeline-ctl.sh reattach <new-session-id> <pipeline-id>`,
  recovering the id from the restored conversation.
- **Inspect:** `pipeline-ctl.sh status <session-id>` | `list` | `resolve <session-id>`.
- **Never adopt another session's pipeline.** Resolution is binding, else a
  directory named for the session id — and nothing else. A project may run
  several pipelines concurrently in different sessions; guessing aims verdicts at
  a pipeline you are not running.

*(This section is self-contained — the multi-pipeline mechanism is fully described
above; no separate protocol file is needed. Migration note: a pre-existing legacy
pipeline at singular `.claude/pipeline.json` is no longer auto-resolved — move it
into `.claude/pipelines/<session-id>/` or complete it before deploying.)*

## Commands

- `/a <task>` — reads manifest, auto-detects whether phases are needed
- `/p <task>` — explicitly invokes planner first, then executes
- `/g` — **user-invocable only** (`disable-model-invocation: true`). It is a
  convenience for the operator, never the required route: after all gates pass
  you commit directly (step 9).

---

## Working preferences

- For **web apps only**: use Selenium to check UI for errors or other problems. Selenium does NOT apply to native desktop apps — for native Windows apps use PyAutoGUI, pywinauto, WinAppDriver, or FlaUI as appropriate.
- On my responses:
    - I communicate in a calm, understated way.
    - I have a casual, conversational communication style.
    - I value authenticity over excessive agreeableness.
    - I express well-supported answers.
    - I offer polite corrections and apply reasoned skepticism when needed.
- SIMPLIFY=FAIL!
- SKIP=FAIL!
- NEVER SIMPLIFY OR SKIP A TEST - JUST FIX THE PROBLEM!
- STOP=FAIL! When there is authorized work with a clear or already-approved next step, DO IT. NEVER pause to report progress and wait, ask permission for the obvious/authorized next step, offer a menu when one option is clearly correct, or "check in" at a milestone. Drive multi-phase tasks and pipelines through ALL phases and gates to completion in ONE continuous push. A completed phase/gate/milestone is NOT a reason to stop — roll straight into the next. Only stop for a GENUINE user-decision blocker: a real fork with material trade-offs, an unauthorized destructive/irreversible action, or a hard technical dead-end. Report by DOING and summarizing after, never by stopping before. **Commit via `git -C <clone> commit -F <msgfile>` yourself; never hand a commit to me.** That form is the sanctioned component-repo pattern, not a bypass — see step 9, which also covers the directory you must be standing in when the final gate fires.
- If I appear to be running on a Windows system that does not have WSL then be sure check for other tools like Python, the Git "bash toolbox" and PowerShell, taking this into account when handling file edits, searches etc.
- Always add UTF-8 unicode support explicitly to python scripts.
- Always use SSH keys when available!
- Always use available agents!
- Whenever **web** UI elements are modified or added, make them "Selenium friendly" for ease of testing. For **native desktop** UI elements, make them automation-friendly for pywinauto/WinAppDriver/FlaUI (stable control IDs, accessible names, AutomationId properties).
