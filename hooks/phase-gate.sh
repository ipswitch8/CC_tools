#!/bin/bash
# .claude/hooks/phase-gate.sh
#
# Fires on Stop and SubagentStop events.
#
# Two responsibilities:
#   1. SubagentStop: if the stopped subagent is a gate agent for the
#      current phase, extract PASS/FAIL from its last_assistant_message
#      and write the result to phase-state.json. This is the ONLY path
#      for writing gate results — the main agent cannot forge them.
#   2. Stop: check gate status and emit the next action (pending gate,
#      /g prompt, or advance).
#
# Hook events: Stop, SubagentStop

set -euo pipefail

PAYLOAD=$(cat)

STATE_FILE=".claude/phase-state.json"
PIPELINE_FILE=".claude/pipeline.json"

DEBUG_LOG=".claude/hook-debug.log"

# Debug: log every invocation
{
  echo "=== $(date -Iseconds) ==="
  echo "HOOK_EVENT: $(echo "$PAYLOAD" | jq -r '.hook_event_name // "Stop"')"
  echo "AGENT_TYPE: $(echo "$PAYLOAD" | jq -r '.agent_type // "(null)"')"
  echo "LAST_MSG_LEN: $(echo "$PAYLOAD" | jq -r '.last_assistant_message // ""' | wc -c)"
  echo "LAST_MSG_TAIL: $(echo "$PAYLOAD" | jq -r '.last_assistant_message // ""' | tail -5)"
  echo "PAYLOAD_KEYS: $(echo "$PAYLOAD" | jq -r 'keys | join(",")')"
  echo "---"
} >> "$DEBUG_LOG" 2>&1

# ── Guarded write: unlock, write, re-lock ─────────────────────────────────────
# phase-state.json is kept read-only at rest so no Bash/Python/script
# indirection can modify it. Only this hook (running as a system process
# on Stop/SubagentStop events) unlocks it temporarily.
write_state() {
  # $1 = jq filter, remaining args = jq options
  local filter="$1"; shift
  chmod +w "$STATE_FILE" 2>/dev/null || true
  jq "$filter" "$@" "$STATE_FILE" > "$STATE_FILE.tmp" && mv "$STATE_FILE.tmp" "$STATE_FILE"
  chmod -w "$STATE_FILE" 2>/dev/null || true
}

# Ensure file is read-only (idempotent — first run after hook install)
chmod -w "$STATE_FILE" 2>/dev/null || true

# ── Not in phased mode ────────────────────────────────────────────────────────
if [ ! -f "$STATE_FILE" ] || [ ! -f "$PIPELINE_FILE" ]; then
  echo '{"continue": true, "abstain": true, "reason": "phase-gate: no pipeline files"}'
  exit 0
fi

# ── Read current state ────────────────────────────────────────────────────────
IDX=$(jq -r '.current_phase_index // 0' "$STATE_FILE")
TOTAL=$(jq '.phases | length' "$PIPELINE_FILE")

# Pipeline complete
if [ "$IDX" -ge "$TOTAL" ]; then
  echo '{"continue": true, "abstain": true, "reason": "phase-gate: pipeline complete"}'
  exit 0
fi

HOOK_EVENT=$(echo "$PAYLOAD" | jq -r '.hook_event_name // "Stop"')

# ── SubagentStop: write gate result from agent output ─────────────────────────
if [ "$HOOK_EVENT" = "SubagentStop" ]; then
  AGENT_TYPE=$(echo "$PAYLOAD" | jq -r '.agent_type // ""')
  LAST_MSG=$(echo "$PAYLOAD" | jq -r '.last_assistant_message // ""')

  # Check if this agent is a gate agent for the current phase
  IS_GATE=$(jq -r --arg a "$AGENT_TYPE" \
    ".phases[$IDX].gate_agents | index(\$a) // -1" "$PIPELINE_FILE")

  if [ "$IS_GATE" != "-1" ] && [ -n "$AGENT_TYPE" ]; then
    # Determine PASS/FAIL from the agent's output.
    # Gate agents emit "PASS" or "FAIL" (case-insensitive) in their
    # last message. Search for these keywords. If ambiguous, default
    # to FAIL (require explicit PASS).
    #
    # ── VERDICT PARSING (v2) ────────────────────────────────────────
    #
    # v1 bug: compared byte offsets of last bare PASS vs FAIL.
    # A multi-check report with per-check "PASS" lines after
    # an overall "FAIL" produced a false-positive.
    #
    # v2: structured verdict lines win. Bare FAIL wins ties.
    # Default is FAIL, not PASS.

    RESULT="remediation"

    # Step 1: structured verdict lines (case-insensitive).
    STRUCTURED_VERDICT=""
    STRUCTURED_LINE=$(echo "$LAST_MSG" | grep -iE '^\*?\*?(VERDICT|RESULT|GATE RESULT|GATE REVIEW)[:\s]*\*?\*?\s*(PASS|FAIL)' | tail -1 || true)
    if [ -n "$STRUCTURED_LINE" ]; then
      if echo "$STRUCTURED_LINE" | grep -qiE 'PASS'; then
        STRUCTURED_VERDICT="PASS"
      fi
      if echo "$STRUCTURED_LINE" | grep -qiE 'FAIL'; then
        STRUCTURED_VERDICT="FAIL"
      fi
    fi

    # Also check markdown-bold standalone: **PASS** or **FAIL**
    if [ -z "$STRUCTURED_VERDICT" ]; then
      BOLD_LINE=$(echo "$LAST_MSG" | grep -iE '\*\*\s*(PASS|FAIL)\s*\*\*' | tail -1 || true)
      if [ -n "$BOLD_LINE" ]; then
        if echo "$BOLD_LINE" | grep -qiE '\*\*\s*PASS\s*\*\*'; then
          STRUCTURED_VERDICT="PASS"
        fi
        if echo "$BOLD_LINE" | grep -qiE '\*\*\s*FAIL\s*\*\*'; then
          STRUCTURED_VERDICT="FAIL"
        fi
      fi
    fi

    # Step 2: apply structured verdict if found.
    if [ "$STRUCTURED_VERDICT" = "PASS" ]; then
      RESULT="true"
    elif [ "$STRUCTURED_VERDICT" = "FAIL" ]; then
      RESULT="remediation"
    else
      # Step 3: no structured verdict — bare keyword with FAIL-wins.
      HAS_FAIL=$(echo "$LAST_MSG" | grep -ciE '\bFAIL\b' || true)
      HAS_PASS=$(echo "$LAST_MSG" | grep -ciE '\bPASS\b' || true)

      if [ "$HAS_FAIL" -gt 0 ]; then
        # ANY bare FAIL → FAIL. This is the key fix.
        RESULT="remediation"
      elif [ "$HAS_PASS" -gt 0 ]; then
        RESULT="true"
      else
        # Neither found → default to FAIL.
        RESULT="remediation"
      fi
    fi

    # Write result to phase-state.json (tagged with phase ID)
    PHASE_ID=$(jq -r ".phases[$IDX].id" "$PIPELINE_FILE")
    if [ "$RESULT" = "true" ]; then
      write_state '.current_gate_results[$agent] = $result | .gate_phase_id = $pid' \
        --arg agent "$AGENT_TYPE" --argjson result true --arg pid "$PHASE_ID"
      echo "{\"continue\": false, \"systemMessage\": \"✅ Gate '${AGENT_TYPE}' recorded PASS (from agent
output).\"}"
    else
      write_state '.current_gate_results[$agent] = $result | .gate_phase_id = $pid' \
        --arg agent "$AGENT_TYPE" --arg result "remediation" --arg pid "$PHASE_ID"
      echo "{\"continue\": true, \"systemMessage\": \"❌ Gate '${AGENT_TYPE}' recorded FAIL → remediation mode.
Fix findings, then re-invoke ${AGENT_TYPE}.\"}"
    fi
    exit 0
  fi

  # Not a gate agent — ignore
  echo '{"continue": true, "abstain": true, "reason": "phase-gate: SubagentStop for non-gate agent"}'
  exit 0
fi

# ── Stop event: loop guard ────────────────────────────────────────────────────
STOP_ACTIVE=$(echo "$PAYLOAD" | jq -r '.stop_hook_active // false')
if [ "$STOP_ACTIVE" = "true" ]; then
  echo '{"continue": true, "abstain": true, "reason": "phase-gate: stop_hook_active loop guard"}'
  exit 0
fi

# ── Re-read state (may have been updated by SubagentStop above) ───────────────
PHASE_NAME=$(jq -r ".phases[$IDX].name" "$PIPELINE_FILE")
PHASE_ID=$(jq -r ".phases[$IDX].id" "$PIPELINE_FILE")
GATE_RESULTS=$(jq -c '.current_gate_results // {}' "$STATE_FILE")
TOTAL_GATES=$(jq ".phases[$IDX].gate_agents | length" "$PIPELINE_FILE")

# ── Stale result detection ────────────────────────────────────────────────────
RESULT_PHASE=$(jq -r '.gate_phase_id // ""' "$STATE_FILE")
RESULT_COUNT=$(echo "$GATE_RESULTS" | jq 'to_entries | length')
if [ "$RESULT_COUNT" -gt 0 ]; then
  if [ -z "$RESULT_PHASE" ] || [ "$RESULT_PHASE" != "$PHASE_ID" ]; then
    write_state '.current_gate_results = {} | del(.gate_phase_id)'
    GATE_RESULTS='{}'
  fi
fi

# ── Find first pending gate (strict array order) ──────────────────────────────
NEXT_GATE=""
COMPLETED_COUNT=0

while IFS= read -r agent; do
  agent=$(printf "%s" "$agent" | tr -d "\r")
  PASSED=$(echo "$GATE_RESULTS" | jq -r --arg a "$agent" \
    '(.[$a] // false) | if type == "object" then (.result // "FAIL") else tostring end')
  if [ "$PASSED" = "true" ] || [ "$PASSED" = "PASS" ]; then
    COMPLETED_COUNT=$((COMPLETED_COUNT + 1))
  elif [ -z "$NEXT_GATE" ]; then
    NEXT_GATE="$agent"
  fi
done < <(jq -r ".phases[$IDX].gate_agents[]" "$PIPELINE_FILE")

# ── All gates passed → advance phase, prompt /g ──────────────────────────────
if [ -z "$NEXT_GATE" ]; then
  NEXT_IDX=$((IDX + 1))

  write_state \
    '.current_phase_index = $next |
     .phases_complete += [$pid] |
     .current_gate_results = {} |
     .awaiting_commit = false' \
    --argjson next "$NEXT_IDX" \
    --arg pid "$PHASE_ID"

  if [ "$NEXT_IDX" -ge "$TOTAL" ]; then
    echo "{\"continue\": true, \"systemMessage\": \"✅ ${PHASE_NAME} — all gates passed (phase advanced). Run /g
 to commit this phase's verified work. Pipeline complete after commit.\"}"
  else
    NEXT_NAME=$(jq -r ".phases[$NEXT_IDX].name" "$PIPELINE_FILE")
    echo "{\"continue\": true, \"systemMessage\": \"✅ ${PHASE_NAME} — all gates passed (phase advanced to
$((NEXT_IDX + 1))/${TOTAL}: ${NEXT_NAME}). Run /g to commit this phase's verified work, then begin the next
phase.\"}"
  fi
  exit 0
fi

# ── Emit next single pending gate ─────────────────────────────────────────────
echo "{\"continue\": true, \"systemMessage\": \"🚦 ${PHASE_NAME} (phase $((IDX + 1))/${TOTAL}) — gate
${COMPLETED_COUNT}/${TOTAL_GATES}: invoke the '${NEXT_GATE}' agent now.\"}"