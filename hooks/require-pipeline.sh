#!/bin/bash
# .claude/hooks/require-pipeline.sh
#
# Fires on UserPromptSubmit.
# Detects multi-phase task prompts and injects a systemMessage instructing
# Claude to invoke the planner before doing any work, if no active pipeline
# exists yet.
#
# Hook event: UserPromptSubmit

set -euo pipefail

PAYLOAD=$(cat)
PROMPT=$(echo "$PAYLOAD" | jq -r '.prompt // ""')

# ── Skip short / conversational prompts ──────────────────────────────────────
WORD_COUNT=$(echo "$PROMPT" | wc -w)
if [ "$WORD_COUNT" -lt 8 ]; then
  echo '{"action": "allow"}'
  exit 0
fi

# ── Skip pipeline management prompts (avoid loops) ───────────────────────────
LOWER=$(echo "$PROMPT" | tr '[:upper:]' '[:lower:]')
if echo "$LOWER" | grep -qE "pipeline\.json|phase-state|planner agent|gate_results"; then
  echo '{"action": "allow"}'
  exit 0
fi

STATE_FILE=".claude/phase-state.json"
PIPELINE_FILE=".claude/pipeline.json"

# ── If pipeline already active and in progress, pass through ─────────────────
if [ -f "$STATE_FILE" ] && [ -f "$PIPELINE_FILE" ]; then
  IDX=$(jq -r '.current_phase_index // 0' "$STATE_FILE" 2>/dev/null || echo "0")
  TOTAL=$(jq '.phases | length' "$PIPELINE_FILE" 2>/dev/null || echo "0")
  if [ "$IDX" -lt "$TOTAL" ]; then
    echo '{"action": "allow"}'
    exit 0
  fi
fi

# ── Detect phase-worthy tasks by signal count ─────────────────────────────────
# Two or more of these signals = likely multi-phase
PHASE_SIGNALS=$(echo "$LOWER" | grep -cE \
  "build|implement|create|develop|migrate|refactor|set up|scaffold|deploy|\
  system|service|api|platform|app|pipeline|architecture|full.stack|\
  phase|step.by.step|first.*then|multiple|end.to.end|integrate|redesign" \
  2>/dev/null || true)

if [ "${PHASE_SIGNALS:-0}" -lt 2 ]; then
  echo '{"action": "allow"}'
  exit 0
fi

# ── Multi-phase task, no active pipeline ─────────────────────────────────────
echo '{
  "action": "allow",
  "systemMessage": "⚠️  This looks like a multi-phase task. MANDATORY: read registry/manifest.json, load the pipeline and meta shards, then invoke the planner agent with the full task description before writing any code. Do not skip this step."
}'
