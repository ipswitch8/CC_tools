#!/usr/bin/env bash
# inject-stop-fail.sh — UserPromptSubmit hook.
#
# Re-injects the STOP=FAIL rule into context on every user prompt so it stays
# salient instead of decaying as the conversation grows. This is the SECONDARY
# control (advisory, but continuously re-asserted). The PRIMARY control is
# stop-work-guard.sh, which is mechanical.
#
# Kept deliberately short — it is paid on every single turn.
set -uo pipefail

STATE_DIR="${HOME}/.claude/state"
payload="$(cat 2>/dev/null || true)"
session="$(printf '%s' "$payload" | jq -r '.session_id // empty' 2>/dev/null || true)"

armed=""
if [ -n "$session" ] && [ -f "${STATE_DIR}/${session}.inprogress" ]; then
  armed="
WORK-GUARD IS ARMED: $(head -c 200 "${STATE_DIR}/${session}.inprogress" 2>/dev/null)
The Stop hook will block a turn-end until you run:
  ~/.claude/hooks/work-guard.sh done"
fi

ctx="STOP=FAIL. A prose summary ENDS THE TURN — that IS stopping. Do not summarise mid-work; chain straight into the next tool call. A completed phase, gate, PR, test run, or milestone is NOT a reason to stop. Stop ONLY for: an unauthorised destructive action, a genuine fork with material trade-offs, or a hard dead-end. One summary at the END of the work.

For multi-step work, arm the guard first:
  ~/.claude/hooks/work-guard.sh begin \"what you are doing\"
and clear it when genuinely finished:
  ~/.claude/hooks/work-guard.sh done${armed}"

jq -nc --arg c "$ctx" \
  '{hookSpecificOutput:{hookEventName:"UserPromptSubmit", additionalContext:$c}}' 2>/dev/null || true
exit 0
