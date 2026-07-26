---
name: a
description: Auto-detect task complexity, load registry shards, and execute with or without phased pipeline
argument-hint: <task description>
disable-model-invocation: true
user-invocable: true
allowed-tools: Read, Grep, Glob, Bash, Agent
---

First, read agents/registry/manifest.json to identify which shards are relevant
to this task. Load only those shards — do not load all shards at once.
Always load the meta shard. Load the pipeline shard for any complex or
multi-step task.

Then evaluate "$ARGUMENTS":

If the task has multiple dependent stages (scaffold -> implement -> test ->
harden, or any sequence where later work verifiably gates earlier work):

  1. Load the pipeline shard from the registry.
  2. Create the pipeline namespace. Determine your session id (newest
     ~/.claude/projects/<encoded-project-dir>/*.jsonl basename minus `.jsonl`,
     or your scratchpad path), then run:
         ~/.claude/hooks/pipeline-ctl.sh init <session-id>
     The pipeline-id defaults to the session id.
  3. Invoke the `planner` agent with "$ARGUMENTS", telling it to write
     pipeline.json INTO `.claude/pipelines/<pipeline-id>/`. Wait for confirmation.
  4. Read `.claude/pipelines/<pipeline-id>/pipeline.json` to understand all phases.
  5. For each phase in index order:
     a. Use appropriate agents from the loaded registry shards.
     b. After phase work, invoke that phase's gate agents one at a time in the
        listed order. Each gate agent's FINAL action must be:
            ~/.claude/hooks/emit-gate-verdict.sh <phase-id> <agent> PASS|FAIL \
              [gtest-xml] [target-test] --pipeline <pipeline-id>
        A gate passes ONLY via that artifact (verdict PASS) — never from prose.
     c. Do not proceed until all gates pass (artifact-verified).
  6. After all gates pass for a phase, run /g to commit; the phase advances
     automatically. See CLAUDE.md "Phased Execution Protocol" for full detail.

Otherwise (single-step or simple task):

  Use appropriate agents from the loaded registry shards to $ARGUMENTS
