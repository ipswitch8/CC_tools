#!/bin/bash
# .claude/hooks/phase-gate.sh
#
# Fires on Stop and SubagentStop events.
#
# v2.1: advance-loop runs from BOTH Stop and SubagentStop, so a
# SubagentStop that completes the current phase's gate set advances
# the pipeline immediately -- no need to wait for an end-of-turn Stop.
# When the main agent runs gates for several phases in one turn, each
# phase advances the moment its last gate flips to PASS, and the loop
# chains through any subsequent phase that already has all gates
# recorded.
#
# v2 (preserved): phase-tagged gate results via PHASE: phase-N echo
# from agents; .gate_results_by_phase keyed by phase ID;
# .current_gate_results / .gate_phase_id maintained as a derived view
# of the active phase for backward compat with pre-tool-use.sh,
# session-resume.sh, and validate-pipeline.sh.

set -euo pipefail

PAYLOAD=$(cat)

STATE_FILE=".claude/phase-state.json"
PIPELINE_FILE=".claude/pipeline.json"

DEBUG_LOG=".claude/hook-debug.log"

{
  echo "=== $(date -Iseconds) ==="
  echo "HOOK_EVENT: $(echo "$PAYLOAD" | jq -r '.hook_event_name // "Stop"')"
  echo "AGENT_TYPE: $(echo "$PAYLOAD" | jq -r '.agent_type // "(null)"')"
  echo "LAST_MSG_LEN: $(echo "$PAYLOAD" | jq -r '.last_assistant_message // ""' | wc -c)"
  echo "LAST_MSG_TAIL: $(echo "$PAYLOAD" | jq -r '.last_assistant_message // ""' | tail -5)"
  echo "PAYLOAD_KEYS: $(echo "$PAYLOAD" | jq -r 'keys | join(",")')"
  echo "---"
} >> "$DEBUG_LOG" 2>&1

LOCK_DIR="${STATE_FILE}.lock"
_acquire_lock() {
  local attempts=0
  while ! mkdir "$LOCK_DIR" 2>/dev/null; do
    attempts=$((attempts + 1))
    if [ "$attempts" -ge 50 ]; then
      rm -rf "$LOCK_DIR" 2>/dev/null
      mkdir "$LOCK_DIR" 2>/dev/null || true
      break
    fi
    sleep 0.1
  done
}
_release_lock() {
  rm -rf "$LOCK_DIR" 2>/dev/null || true
}
write_state() {
  local filter="$1"; shift
  _acquire_lock
  chmod +w "$STATE_FILE" 2>/dev/null || true
  jq "$filter" "$@" "$STATE_FILE" > "$STATE_FILE.tmp" && mv "$STATE_FILE.tmp" "$STATE_FILE"
  chmod -w "$STATE_FILE" 2>/dev/null || true
  _release_lock
}

chmod -w "$STATE_FILE" 2>/dev/null || true

if [ ! -f "$STATE_FILE" ] || [ ! -f "$PIPELINE_FILE" ]; then
  echo '{"continue": true, "abstain": true, "reason": "phase-gate: no pipeline files"}'
  exit 0
fi

# v2.3: sig-check guard. If the state file's recorded
# content_sig doesn't match the current pipeline.json's computed
# sig, the state is stale (carried over from a prior pipeline).
# Abstain with an explicit message rather than silently treating
# IDX>=TOTAL as "pipeline complete". validate-pipeline.sh will
# reset state on its next Stop trigger; until then, do nothing
# with state so we don't accept gate results against the wrong
# pipeline or block operations under a stale "complete" status.
#
# This closes the SAVE-08 -> D2 cross-pipeline ID collision bug:
# both pipelines used phase-1...phase-6, IDX from SAVE-08 (=6)
# matched TOTAL of D2 (=6), and the gate hook silently abstained
# every time a gate fired against the new D2 pipeline.
PIPELINE_SIG_NOW=$(jq -r '[.phases[] | (.id + ":" + .name)] | join("|")' "$PIPELINE_FILE" 2>/dev/null | sha256sum | cut -d' ' -f1)
STATE_SIG_NOW=$(jq -r '.pipeline_content_sig // ""' "$STATE_FILE" 2>/dev/null)
if [ -n "$PIPELINE_SIG_NOW" ] && [ -n "$STATE_SIG_NOW" ] && [ "$PIPELINE_SIG_NOW" != "$STATE_SIG_NOW" ]; then
  echo "{\"continue\": true, \"systemMessage\": \"phase-gate: pipeline content_sig mismatch (state recorded ${STATE_SIG_NOW:0:8}, pipeline now ${PIPELINE_SIG_NOW:0:8}). State is stale from a prior pipeline. Wait for the next Stop event so the pipeline-validator can reset, then re-invoke gates.\"}"
  exit 0
fi

IDX=$(jq -r '.current_phase_index // 0' "$STATE_FILE")
TOTAL=$(jq '.phases | length' "$PIPELINE_FILE")

if [ "$IDX" -ge "$TOTAL" ]; then
  echo '{"continue": true, "abstain": true, "reason": "phase-gate: pipeline complete"}'
  exit 0
fi

# v1 -> v2 schema migration (idempotent)
HAS_NEW=$(jq -r 'has("gate_results_by_phase")' "$STATE_FILE")
if [ "$HAS_NEW" != "true" ]; then
  OLD_PHASE=$(jq -r '.gate_phase_id // ""' "$STATE_FILE")
  OLD_RESULTS=$(jq -c '.current_gate_results // {}' "$STATE_FILE")
  if [ -n "$OLD_PHASE" ] && [ "$OLD_RESULTS" != "{}" ]; then
    write_state '.gate_results_by_phase = {($pid): $r}' --arg pid "$OLD_PHASE" --argjson r "$OLD_RESULTS"
  else
    write_state '.gate_results_by_phase = {}'
  fi
fi

# ── v2.2: shared Bash-gate list + nonce-issue helper ──────────────────────────
# Single source of truth for which gates need shell access. Used by both
# the SubagentStop PASS path (new in v2.2) and the Stop event tail.
BASH_GATES="test-runner perf-benchmarks"
BASH_UNLOCK_FILE=".claude/gate-bash-unlock"

issue_bash_unlock_if_needed() {
  local next_gate="$1"
  if [ -z "$next_gate" ]; then
    rm -f "$BASH_UNLOCK_FILE" 2>/dev/null || true
    return
  fi
  if echo "$BASH_GATES" | grep -qw "$next_gate"; then
    local expires
    expires=$(date -d "+5 minutes" +%s 2>/dev/null || date -v+5M +%s 2>/dev/null || echo "0")
    echo "{\"gate\":\"${next_gate}\",\"expires\":${expires}}" > "$BASH_UNLOCK_FILE"
    chmod -w "$BASH_UNLOCK_FILE" 2>/dev/null || true
  else
    rm -f "$BASH_UNLOCK_FILE" 2>/dev/null || true
  fi
}
  
# ── Multi-phase advance helper ────────────────────────────────────────────────
ADVANCED_PHASES=()
NEXT_GATE=""
COMPLETED_COUNT=0

advance_loop() {
  ADVANCED_PHASES=()
  NEXT_GATE=""
  COMPLETED_COUNT=0
  IDX=$(jq -r '.current_phase_index // 0' "$STATE_FILE")
  while [ "$IDX" -lt "$TOTAL" ]; do
    local phase_id phase_results all_pass=true next_gate="" completed=0
    phase_id=$(jq -r ".phases[$IDX].id" "$PIPELINE_FILE")
    phase_results=$(jq -c --arg p "$phase_id" '.gate_results_by_phase[$p] // {}' "$STATE_FILE")

    while IFS= read -r agent; do
      agent=$(printf "%s" "$agent" | tr -d "\r")
      local passed
      passed=$(echo "$phase_results" | jq -r --arg a "$agent" \
        '(.[$a] // false) | if type == "object" then (.result // "FAIL") else tostring end')
      if [ "$passed" = "true" ] || [ "$passed" = "PASS" ]; then
        completed=$((completed + 1))
      else
        all_pass=false
        [ -z "$next_gate" ] && next_gate="$agent"
      fi
    done < <(jq -r ".phases[$IDX].gate_agents[]" "$PIPELINE_FILE")

    if [ "$all_pass" = "true" ]; then
      local next_idx=$((IDX + 1))
      local next_phase_id
      next_phase_id=$(jq -r ".phases[$next_idx].id // \"\"" "$PIPELINE_FILE" 2>/dev/null)
      write_state '
        .current_phase_index = $next |
        .phases_complete += [$pid] |
        .awaiting_commit = false |
        .gate_phase_id = $npid |
        .current_gate_results = (.gate_results_by_phase[$npid] // {})
      ' --argjson next "$next_idx" --arg pid "$phase_id" --arg npid "$next_phase_id"
      ADVANCED_PHASES+=("$phase_id")
      rm -f ".claude/gate-bash-unlock" 2>/dev/null || true
      IDX="$next_idx"
    else
      NEXT_GATE="$next_gate"
      COMPLETED_COUNT="$completed"
      break
    fi
  done
}

build_advance_message() {
  if [ "${#ADVANCED_PHASES[@]}" -eq 0 ]; then
    echo ""
    return
  fi
  local advanced_list
  advanced_list=$(printf '%s, ' "${ADVANCED_PHASES[@]}")
  advanced_list="${advanced_list%, }"
  if [ "$IDX" -ge "$TOTAL" ]; then
    echo "OK Gates passed for: ${advanced_list}. Pipeline complete. Run /g to commit verified work."
  else
    local next_name
    next_name=$(jq -r ".phases[$IDX].name" "$PIPELINE_FILE")
    echo "OK Gates passed for: ${advanced_list}. Advanced to phase $((IDX + 1))/${TOTAL}: ${next_name}. Run /g to commit verified work."
  fi
}

HOOK_EVENT=$(echo "$PAYLOAD" | jq -r '.hook_event_name // "Stop"')

# ── SubagentStop ──────────────────────────────────────────────────────────────
if [ "$HOOK_EVENT" = "SubagentStop" ]; then
  AGENT_TYPE=$(echo "$PAYLOAD" | jq -r '.agent_type // ""')
  LAST_MSG=$(echo "$PAYLOAD" | jq -r '.last_assistant_message // ""')

  PARSED_PHASE=""
  PHASE_TAG_LINE=$(echo "$LAST_MSG" | grep -oiE '\*\*?\s*(PHASE|TARGET[ _]?PHASE|GATE[ _]?PHASE)[: ]+\*?\*?[[:space:]]*phase-[0-9]+' | tail -1 || true)
  if [ -n "$PHASE_TAG_LINE" ]; then
    PARSED_PHASE=$(echo "$PHASE_TAG_LINE" | grep -oE 'phase-[0-9]+' | head -1)
    EXISTS=$(jq -r --arg p "$PARSED_PHASE" \
      '.phases[] | select(.id == $p) | .id' "$PIPELINE_FILE" 2>/dev/null || true)
    [ -z "$EXISTS" ] && PARSED_PHASE=""
  fi
  TARGET_PHASE_ID="${PARSED_PHASE:-$(jq -r ".phases[$IDX].id" "$PIPELINE_FILE")}"

  IS_GATE=$(jq -r --arg a "$AGENT_TYPE" --arg p "$TARGET_PHASE_ID" \
    '.phases[] | select(.id == $p) | .gate_agents | index($a) // -1' \
    "$PIPELINE_FILE" 2>/dev/null || echo "-1")

  if [ "$IS_GATE" != "-1" ] && [ -n "$AGENT_TYPE" ]; then
    RESULT="remediation"

    STRUCTURED_VERDICT=""
    STRUCTURED_LINE=$(echo "$LAST_MSG" | grep -iE '^\*?\*?(VERDICT|RESULT|GATE RESULT|GATE REVIEW)[:\s]*\*?\*?\s*(PASS|FAIL)' | tail -1 || true)
    if [ -n "$STRUCTURED_LINE" ]; then
      if echo "$STRUCTURED_LINE" | grep -qiE 'PASS'; then STRUCTURED_VERDICT="PASS"; fi
      if echo "$STRUCTURED_LINE" | grep -qiE 'FAIL'; then STRUCTURED_VERDICT="FAIL"; fi
    fi
    if [ -z "$STRUCTURED_VERDICT" ]; then
      BOLD_LINE=$(echo "$LAST_MSG" | grep -iE '\*\*\s*(PASS|FAIL)\s*\*\*' | tail -1 || true)
      if [ -n "$BOLD_LINE" ]; then
        if echo "$BOLD_LINE" | grep -qiE '\*\*\s*PASS\s*\*\*'; then STRUCTURED_VERDICT="PASS"; fi
        if echo "$BOLD_LINE" | grep -qiE '\*\*\s*FAIL\s*\*\*'; then STRUCTURED_VERDICT="FAIL"; fi
      fi
    fi

    if [ "$STRUCTURED_VERDICT" = "PASS" ]; then
      RESULT="true"
    elif [ "$STRUCTURED_VERDICT" = "FAIL" ]; then
      RESULT="remediation"
    else
      HAS_FAIL=$(echo "$LAST_MSG" | grep -ciE '\bFAIL\b' || true)
      HAS_PASS=$(echo "$LAST_MSG" | grep -ciE '\bPASS\b' || true)
      if [ "$HAS_FAIL" -gt 0 ]; then
        RESULT="remediation"
      elif [ "$HAS_PASS" -gt 0 ]; then
        RESULT="true"
      else
        RESULT="remediation"
      fi
    fi

    CURR_PHASE_ID=$(jq -r ".phases[$IDX].id" "$PIPELINE_FILE")
    rm -f ".claude/gate-bash-unlock" 2>/dev/null || true

    if [ "$RESULT" = "true" ]; then
      write_state '
        .gate_results_by_phase[$tpid][$agent] = $result |
        (if $tpid == $cpid then
            .current_gate_results[$agent] = $result |
            .gate_phase_id = $cpid
         else . end)
      ' --arg agent "$AGENT_TYPE" --argjson result true \
        --arg tpid "$TARGET_PHASE_ID" --arg cpid "$CURR_PHASE_ID"

      # v2.1: try to advance immediately on PASS.
      advance_loop
      ADV_MSG=$(build_advance_message)
      if [ -n "$ADV_MSG" ]; then
        echo "{\"continue\": false, \"systemMessage\": \"${ADV_MSG}\"}"
      else
        # v2.2: re-issue Bash unlock if the next pending gate needs it,
        # so chained karen → validator → test-runner works in one turn.
        issue_bash_unlock_if_needed "$NEXT_GATE"
        echo "{\"continue\": false, \"systemMessage\": \"OK Gate '${AGENT_TYPE}' recorded PASS for ${TARGET_PHASE_ID}.\"}"
      fi
    else
      write_state '
        .gate_results_by_phase[$tpid][$agent] = $result |
        (if $tpid == $cpid then
            .current_gate_results[$agent] = $result |
            .gate_phase_id = $cpid
         else . end)
      ' --arg agent "$AGENT_TYPE" --arg result "remediation" \
        --arg tpid "$TARGET_PHASE_ID" --arg cpid "$CURR_PHASE_ID"
      echo "{\"continue\": true, \"systemMessage\": \"FAIL Gate '${AGENT_TYPE}' recorded FAIL for ${TARGET_PHASE_ID} -> remediation. Fix findings, then re-invoke ${AGENT_TYPE}.\"}"
    fi
    exit 0
  fi

  echo '{"continue": true, "abstain": true, "reason": "phase-gate: SubagentStop for non-gate agent"}'
  exit 0
fi

# ── Stop event: loop guard ────────────────────────────────────────────────────
STOP_ACTIVE=$(echo "$PAYLOAD" | jq -r '.stop_hook_active // false')
if [ "$STOP_ACTIVE" = "true" ]; then
  echo '{"continue": true, "abstain": true, "reason": "phase-gate: stop_hook_active loop guard"}'
  exit 0
fi

# ── Stop event: run advance loop ──────────────────────────────────────────────
advance_loop
ADV_MSG=$(build_advance_message)

if [ -n "$ADV_MSG" ]; then
  echo "{\"continue\": true, \"systemMessage\": \"${ADV_MSG}\"}"
  exit 0
fi

# No advance -> emit pending-gate prompt with PHASE-tag instruction
PHASE_NAME=$(jq -r ".phases[$IDX].name" "$PIPELINE_FILE")
PHASE_ID=$(jq -r ".phases[$IDX].id" "$PIPELINE_FILE")
TOTAL_GATES=$(jq ".phases[$IDX].gate_agents | length" "$PIPELINE_FILE")

issue_bash_unlock_if_needed "$NEXT_GATE"

echo "{\"continue\": true, \"systemMessage\": \"GATE ${PHASE_NAME} (phase $((IDX + 1))/${TOTAL}) - gate ${COMPLETED_COUNT}/${TOTAL_GATES}: invoke '${NEXT_GATE}' agent. Ask the agent to echo **PHASE: ${PHASE_ID}** in its verdict block so the result is tagged correctly.\"}"