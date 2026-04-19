#!/bin/bash
set -euo pipefail
PAYLOAD=$(cat)
STOP_ACTIVE=$(echo "$PAYLOAD" | jq -r '.stop_hook_active // false')
if [ "$STOP_ACTIVE" = "true" ]; then
  echo '{"continue": true, "abstain": true, "reason": "validate-pipeline: stop_hook_active"}'
  exit 0
fi
STATE_FILE=".claude/phase-state.json"
PIPELINE_FILE=".claude/pipeline.json"
if [ ! -f "$PIPELINE_FILE" ]; then
  echo '{"continue": true, "abstain": true, "reason": "validate-pipeline: no pipeline file"}'
  exit 0
fi
NEEDS_RESET=false
if [ ! -f "$STATE_FILE" ]; then
  NEEDS_RESET=true
elif ! jq -e '.phases_complete' "$STATE_FILE" >/dev/null 2>&1; then
  NEEDS_RESET=true
else
  # Primary check: compare generated_at timestamps.
  # If pipeline.json has a generated_at that differs from what
  # phase-state.json recorded, this is a different pipeline.
  PIPELINE_GEN=$(jq -r '.generated_at // ""' "$PIPELINE_FILE" 2>/dev/null)
  STATE_GEN=$(jq -r '.pipeline_generated_at // ""' "$STATE_FILE" 2>/dev/null)
  if [ -n "$PIPELINE_GEN" ] && [ "$PIPELINE_GEN" != "$STATE_GEN" ]; then
    NEEDS_RESET=true
  fi
  # Fallback checks (for pipelines without generated_at, or manual edits)
  if [ "$NEEDS_RESET" = "false" ]; then
    PIPELINE_TOTAL=$(jq '.phases | length' "$PIPELINE_FILE" 2>/dev/null)
    STATE_COMPLETED=$(jq -c '.phases_complete // []' "$STATE_FILE" 2>/dev/null)
    STATE_IDX=$(jq -r '.current_phase_index // 0' "$STATE_FILE" 2>/dev/null)
    STALE_PHASES=$(jq -r --argjson completed "$STATE_COMPLETED" \
      '[.phases[].id] as $valid | [$completed[] | select(. as $c | $valid | index($c) | not)] | length' \
      "$PIPELINE_FILE" 2>/dev/null || echo "0")
    if [ "$STALE_PHASES" -gt 0 ]; then
      NEEDS_RESET=true
    fi
    if [ "$STATE_IDX" -ge "$PIPELINE_TOTAL" ] 2>/dev/null; then
      LAST_PHASE_ID=$(jq -r '.phases[-1].id // ""' "$PIPELINE_FILE" 2>/dev/null)
      HAS_LAST=$(echo "$STATE_COMPLETED" | jq --arg id "$LAST_PHASE_ID" 'index($id) // -1')
      if [ "$HAS_LAST" = "-1" ]; then
        NEEDS_RESET=true
      fi
    fi
  fi
fi
if [ "$NEEDS_RESET" = "true" ]; then
  PIPELINE_GEN=$(jq -r '.generated_at // ""' "$PIPELINE_FILE" 2>/dev/null)
  chmod +w "$STATE_FILE" 2>/dev/null || true
  jq -n --arg gen "$PIPELINE_GEN" '{
    pipeline: ".claude/pipeline.json",
    pipeline_generated_at: $gen,
    current_phase_index: 0,
    phases_complete: [],
    current_gate_results: {},
    awaiting_commit: false,
    gate_phase_id: ""
  }' > "$STATE_FILE"
  chmod -w "$STATE_FILE" 2>/dev/null || true
  PHASE_COUNT=$(jq '.phases | length' "$PIPELINE_FILE")
  echo "{\"continue\": true, \"systemMessage\": \"Stale state detected (generated_at mismatch or invalid phase
IDs). Auto-reset to index 0 for current pipeline (${PHASE_COUNT} phases).\"}"
  exit 0
fi
IDX=$(jq -r '.current_phase_index // -1' "$STATE_FILE" 2>/dev/null)
COMPLETED=$(jq -r '.phases_complete | length' "$STATE_FILE" 2>/dev/null || echo "1")
if [ "$IDX" != "0" ] || [ "$COMPLETED" != "0" ]; then
  echo '{"continue": true, "abstain": true, "reason": "validate-pipeline: past initial phase"}'
  exit 0
fi
VALID=$(jq 'type == "object" and has("phases") and (.phases | type == "array") and (.phases | length > 0) and
(.phases | all(has("id") and has("name") and has("gate_agents") and (.gate_agents | type == "array") and
(.gate_agents | length > 0)))' "$PIPELINE_FILE" 2>/dev/null || echo "false")
if [ "$VALID" != "true" ]; then
  echo '{"continue": true, "systemMessage": "pipeline.json is malformed. Re-invoke the planner agent."}'
  exit 0
fi
PHASE_COUNT=$(jq '.phases | length' "$PIPELINE_FILE")
PHASE_NAMES=$(jq -r '[.phases[].name] | join(" -> ")' "$PIPELINE_FILE")
TASK=$(jq -r '.task // "unspecified"' "$PIPELINE_FILE")
echo "{\"continue\": false, \"systemMessage\": \"Pipeline validated: ${PHASE_COUNT} phases. Sequence:
${PHASE_NAMES}. Begin Phase 1.\"}"