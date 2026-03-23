First, read registry/manifest.json to identify which shards are relevant
to this task. Load only those shards — do not load all shards at once.
Always load the meta shard. Load the pipeline shard for any complex or
multi-step task.

Then evaluate "$ARGUMENTS":

If the task has multiple dependent stages (scaffold → implement → test →
harden, or any sequence where later work verifiably gates earlier work):

  1. Load the pipeline shard from the registry
  2. Invoke the `planner` agent with: "$ARGUMENTS"
  3. Wait for pipeline.json to be written and confirmed
  4. For each phase in index order:
     a. Use appropriate agents from the loaded registry shards
     b. After phase work, invoke gate agents listed in that phase's
        pipeline.json entry — one at a time in listed order
     c. Do not proceed until all gates pass

Otherwise (single-step or simple task):

  Use appropriate agents from the loaded registry shards to $ARGUMENTS
