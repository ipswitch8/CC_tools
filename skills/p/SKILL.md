---
name: p
description: Explicitly invoke planner first, then execute phased pipeline from registry shards
argument-hint: <task description>
disable-model-invocation: true
user-invocable: true
allowed-tools: Read, Grep, Glob, Bash, Agent
---

Read registry/manifest.json to identify which shards are relevant to this
task. Always load the pipeline and meta shards. Load additional shards
based on the task domain.

Then:

1. Invoke the `planner` agent with this task description: "$ARGUMENTS"
   Wait for planner to confirm pipeline.json is written and report the
   phase count and sequence.

2. For each phase in the order defined in pipeline.json:
   a. Use appropriate agents from the loaded registry shards to complete
      the phase work
   b. After phase work, invoke gate agents listed in that phase's entry —
      one at a time in listed order — before proceeding
   c. Do not advance until all gate agents have passed

Do not begin phase work until pipeline.json exists and is confirmed.
