#!/bin/bash
# .claude/hooks/validate-pipeline.sh
#
# Fires on Stop events.
# Only active at phase index 0 with no completed phases (pre-Phase 1).
# Validates pipeline.json structure and reports the plan before work starts.
#
# Hook event: Stop

set -euo pipefail

PAYLOAD=$(cat)

# ── Loop guard ────────────────────────────────────────────────────────────────
STOP_ACTIVE=$(echo "$PAYLOAD" | jq -r '.stop_hook_active // false')
if [ "$STOP_ACTIVE" = "true" ]; then
  echo '{"continue": false}'
  exit 0
fi

STATE_FILE=".claude/phase-state.json"
PIPELINE_FILE=".claude/pipeline.json"

if [ ! -f "$PIPELINE_FILE" ] || [ ! -f "$STATE_FILE" ]; then
  echo '{"continue": false}'
  exit 0
fi

# ── Only run at the very start ────────────────────────────────────────────────
IDX=$(jq -r '.current_phase_index // -1' "$STATE_FILE" 2>/dev/null)
COMPLETED=$(jq -r '.phases_complete | length' "$STATE_FILE" 2>/dev/null || echo "1")

if [ "$IDX" != "0" ] || [ "$COMPLETED" != "0" ]; then
  echo '{"continue": false}'
  exit 0
fi

# ── Validate structure ────────────────────────────────────────────────────────
VALID=$(jq '
  type == "object" and
  has("phases") and
  (.phases | type == "array") and
  (.phases | length > 0) and
  (.phases | all(
    has("id") and has("name") and
    has("gate_agents") and
    (.gate_agents | type == "array") and
    (.gate_agents | length > 0)
  ))
' "$PIPELINE_FILE" 2>/dev/null || echo "false")

if [ "$VALID" != "true" ]; then
  echo '{
    "continue": true,
    "systemMessage": "❌ pipeline.json is missing or malformed. Re-invoke the planner agent before starting Phase 1. Required: { phases: [{ id, name, gate_agents: [...] }] }"
  }'
  exit 0
fi

# ── Valid — report summary ────────────────────────────────────────────────────
PHASE_COUNT=$(jq '.phases | length' "$PIPELINE_FILE")
PHASE_NAMES=$(jq -r '[.phases[].name] | join(" → ")' "$PIPELINE_FILE")
TASK=$(jq -r '.task // "unspecified"' "$PIPELINE_FILE")

echo "{\"continue\": false, \"systemMessage\": \"✅ Pipeline validated: ${PHASE_COUNT} phases for task: ${TASK}. Sequence: ${PHASE_NAMES}. Load appropriate registry shards and begin Phase 1.\"}"
