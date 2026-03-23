#!/bin/bash
# .claude/hooks/session-resume.sh
#
# Fires on SessionStart (trigger: "init").
# If a pipeline is in progress, reinjects current state into context so
# Claude knows exactly where execution left off after a session restart.
#
# Hook event: SessionStart

set -euo pipefail

PAYLOAD=$(cat)
TRIGGER=$(echo "$PAYLOAD" | jq -r '.trigger // ""')

if [ "$TRIGGER" != "init" ]; then
  exit 0
fi

STATE_FILE=".claude/phase-state.json"
PIPELINE_FILE=".claude/pipeline.json"

if [ ! -f "$STATE_FILE" ] || [ ! -f "$PIPELINE_FILE" ]; then
  exit 0
fi

IDX=$(jq -r '.current_phase_index // 0' "$STATE_FILE" 2>/dev/null || echo "0")
TOTAL=$(jq '.phases | length' "$PIPELINE_FILE" 2>/dev/null || echo "0")

# Pipeline complete — nothing to resume
if [ "$IDX" -ge "$TOTAL" ]; then
  exit 0
fi

PHASE_NAME=$(jq -r ".phases[$IDX].name" "$PIPELINE_FILE")
PHASE_DESC=$(jq -r ".phases[$IDX].description // \"\"" "$PIPELINE_FILE")
COMPLETED=$(jq -r '[.phases_complete[]] | if length == 0 then "none" else join(", ") end' "$STATE_FILE" 2>/dev/null || echo "none")
GATE_RESULTS=$(jq -c '.current_gate_results // {}' "$STATE_FILE")

NEXT_GATE=$(jq -r \
  --argjson idx "$IDX" \
  --argjson results "$GATE_RESULTS" \
  '.phases[$idx].gate_agents | map(select(. as $a | ($results[$a] // false) != true)) | first // "none"' \
  "$PIPELINE_FILE" 2>/dev/null || echo "unknown")

TASK=$(jq -r '.task // "unknown task"' "$PIPELINE_FILE")

CONTEXT="📋 PIPELINE RESUME — Session restarted mid-execution.

Task: ${TASK}
Progress: Phase $((IDX + 1))/${TOTAL} — ${PHASE_NAME}
Phases complete: ${COMPLETED}
Current phase: ${PHASE_NAME} — ${PHASE_DESC}
Next pending gate: ${NEXT_GATE}

Read .claude/pipeline.json for the full plan and .claude/phase-state.json
for current gate results. Load registry/manifest.json to identify needed
shards. Resume from current phase — do not restart completed phases."

echo "{\"additionalContext\": \"${CONTEXT}\"}"
