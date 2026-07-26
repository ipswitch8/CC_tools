---
name: p
description: Explicitly invoke planner first, then execute phased pipeline from registry shards
argument-hint: <task description>
disable-model-invocation: true
user-invocable: true
allowed-tools: Read, Grep, Glob, Bash, Agent
---

Read agents/registry/manifest.json to identify which shards are relevant to this
task. Always load the pipeline and meta shards. Load additional shards
based on the task domain.

Then:

1. Create the pipeline namespace. Determine your session id (newest
   ~/.claude/projects/<encoded-project-dir>/*.jsonl basename minus `.jsonl`, or
   your scratchpad path), then run:
       ~/.claude/hooks/pipeline-ctl.sh init <session-id>
   The pipeline-id defaults to the session id.

2. Invoke the `planner` agent with this task description: "$ARGUMENTS", telling
   it to write pipeline.json INTO `.claude/pipelines/<pipeline-id>/`. Wait for
   planner to confirm it is written and report the phase count and sequence.

3. Read `.claude/pipelines/<pipeline-id>/pipeline.json`. For each phase in the
   order it defines:
   a. Use appropriate agents from the loaded registry shards to complete
      the phase work.
   b. After phase work, invoke that phase's gate agents one at a time in the
      listed order. Each gate agent's FINAL action must be:
          ~/.claude/hooks/emit-gate-verdict.sh <phase-id> <agent> PASS|FAIL \
            [gtest-xml] [target-test] --pipeline <pipeline-id>
      A gate passes ONLY via that artifact (verdict PASS) — never from prose.
   c. Do not advance until all gate agents have passed (artifact-verified).

4. After all gates pass for a phase, run /g to commit; the phase advances
   automatically. See CLAUDE.md "Phased Execution Protocol" for full detail.

Do not begin phase work until pipeline.json exists in the namespace and is confirmed.
