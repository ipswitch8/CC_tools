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

BLOB=$(cat)
PROMPT=$(echo "$BLOB" | jq -r '.prompt // ""')

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

# Resolve the per-session pipeline base (namespaced under .claude/pipelines/<id>/).
# The old singular ".claude/pipeline.json" paths predate namespacing, so an active
# pipeline was never detected and this hook kept re-nagging to invoke the planner.
_SID=$(printf '%s' "$BLOB" | jq -r '.session_id // ""' 2>/dev/null || echo "")
if [ -f "$(dirname "${BASH_SOURCE[0]}")/pipeline-lib.sh" ]; then
  . "$(dirname "${BASH_SOURCE[0]}")/pipeline-lib.sh"
  _BASE=$(resolve_pipeline_base "$_SID" 2>/dev/null || echo ".claude")
else _BASE=".claude"; fi
[ -n "$_BASE" ] || _BASE=".claude"
STATE_FILE="$_BASE/phase-""state.json"
PIPELINE_FILE="$_BASE/pipeline.json"

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
# Two or more of these signal words = likely multi-phase.
# Count OCCURRENCES (grep -oE | wc -l), not matching lines. The old `grep -cE`
# counted matching LINES, so a normal single-line prompt maxed out at 1 and the
# `-lt 2` threshold below could never trip -- the nag effectively never fired.
# (Pattern kept on one line so no stray leading whitespace leaks into the
# alternation; LOWER is already lowercased so "e2e" matches "E2E".)
PHASE_SIGNALS=$(echo "$LOWER" | grep -oE \
  "build|implement|create|develop|migrate|refactor|set up|scaffold|deploy|system|service|api|platform|app|pipeline|architecture|full.stack|phase|step.by.step|first.*then|multiple|end.to.end|e2e|integrate|redesign" \
  2>/dev/null | wc -l || true)

if [ "${PHASE_SIGNALS:-0}" -lt 2 ]; then
  echo '{"action": "allow"}'
  exit 0
fi

# ── Multi-phase task, no active pipeline ─────────────────────────────────────
echo '{
  "action": "allow",
  "systemMessage": "⚠️  This looks like a multi-phase task. MANDATORY: read agents/registry/manifest.json, load the pipeline and meta shards, then invoke the planner agent with the full task description before writing any code. Do not skip this step."
}'
