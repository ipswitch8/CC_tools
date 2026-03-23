#!/bin/bash
# .claude/hooks/phase-gate.sh
#
# Fires on Stop and SubagentStop events.
# Reads pipeline.json + phase-state.json to determine whether gate agents
# have all passed for the current phase. Emits ONE pending gate at a time
# to enforce strict sequential execution.
#
# Actions:
#   - All gates passed  → advance phase index, clear gate results
#   - Gates pending     → emit next single pending gate as systemMessage
#   - Pipeline complete → silent exit
#
# Hook events: Stop, SubagentStop

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

# ── Not in phased mode ────────────────────────────────────────────────────────
if [ ! -f "$STATE_FILE" ] || [ ! -f "$PIPELINE_FILE" ]; then
  echo '{"continue": false}'
  exit 0
fi

# ── Read current state ────────────────────────────────────────────────────────
IDX=$(jq -r '.current_phase_index // 0' "$STATE_FILE")
TOTAL=$(jq '.phases | length' "$PIPELINE_FILE")

# Pipeline complete
if [ "$IDX" -ge "$TOTAL" ]; then
  echo '{"continue": false}'
  exit 0
fi

PHASE_NAME=$(jq -r ".phases[$IDX].name" "$PIPELINE_FILE")
PHASE_ID=$(jq -r ".phases[$IDX].id" "$PIPELINE_FILE")
GATE_RESULTS=$(jq -c '.current_gate_results // {}' "$STATE_FILE")
TOTAL_GATES=$(jq ".phases[$IDX].gate_agents | length" "$PIPELINE_FILE")

# ── Find first pending gate (strict array order) ──────────────────────────────
NEXT_GATE=""
COMPLETED_COUNT=0

while IFS= read -r agent; do
  PASSED=$(echo "$GATE_RESULTS" | jq -r --arg a "$agent" '.[$a] // false')
  if [ "$PASSED" = "true" ]; then
    COMPLETED_COUNT=$((COMPLETED_COUNT + 1))
  elif [ -z "$NEXT_GATE" ]; then
    NEXT_GATE="$agent"
  fi
done < <(jq -r ".phases[$IDX].gate_agents[]" "$PIPELINE_FILE")

# ── All gates passed → advance ────────────────────────────────────────────────
if [ -z "$NEXT_GATE" ]; then
  NEXT_IDX=$((IDX + 1))

  jq \
    --argjson next "$NEXT_IDX" \
    --arg pid "$PHASE_ID" \
    '.current_phase_index = $next |
     .phases_complete += [$pid] |
     .current_gate_results = {}' \
    "$STATE_FILE" > "$STATE_FILE.tmp" && mv "$STATE_FILE.tmp" "$STATE_FILE"

  if [ "$NEXT_IDX" -ge "$TOTAL" ]; then
    echo "{\"continue\": false, \"systemMessage\": \"✅ All ${TOTAL} phases complete. Pipeline finished.\"}"
  else
    NEXT_NAME=$(jq -r ".phases[$NEXT_IDX].name" "$PIPELINE_FILE")
    echo "{\"continue\": true, \"systemMessage\": \"✅ ${PHASE_NAME} — all gates passed. Advancing to phase $((NEXT_IDX + 1))/${TOTAL}: ${NEXT_NAME}. Load the appropriate registry shards and begin phase work.\"}"
  fi
  exit 0
fi

# ── Emit next single pending gate ─────────────────────────────────────────────
echo "{\"continue\": true, \"systemMessage\": \"🚦 ${PHASE_NAME} (phase $((IDX + 1))/${TOTAL}) — gate ${COMPLETED_COUNT}/${TOTAL_GATES}: invoke the '${NEXT_GATE}' agent now. Wait for it to write its result to phase-state.json before invoking any other gate agent.\"}"
