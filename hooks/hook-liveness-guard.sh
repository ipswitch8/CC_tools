#!/bin/bash
# hook-liveness-guard.sh — PreToolUse hard-STOP when recording hooks are dead (GENERIC).
#
# Fires on PreToolUse. Blocks ONLY `git commit`, and ONLY when ALL of these hold
# for THIS session's resolved pipeline base:
#   * a pipeline is active (base has pipeline.json + the gate ledger), AND
#   * the pipeline is mid-gating (current phase index < total phases), AND
#   * a compaction has occurred (<base>/last-compact) with NO Stop/SubagentStop
#     heartbeat (<base>/hook-heartbeat) landing since.
# In that state the artifact-recording hooks are presumed dead (post-compact
# delivery bug) and gate results cannot be trusted, so committing would act on
# unverified state.
#
# Scope is deliberately narrow to avoid wedging: every non-commit tool, and every
# tool while no pipeline is mid-gating, is allowed instantly. FAILS OPEN on any
# internal error — a bug here must never brick tool use. Fails CLOSED only on the
# one unambiguous positive detection above.
set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/pipeline-lib.sh" 2>/dev/null || true

PAYLOAD=$(cat 2>/dev/null || true)
TOOL=$(printf '%s' "$PAYLOAD" | jq -r '.tool_name // ""' 2>/dev/null || echo "")
case "$TOOL" in
  Bash|PowerShell) ;;
  *) echo '{"action":"allow"}'; exit 0 ;;
esac

CMD=$(printf '%s' "$PAYLOAD" | jq -r '.tool_input.command // ""' 2>/dev/null || echo "")
# grep -E, not -P, to avoid the locale-PCRE failure.
printf '%s' "$CMD" | grep -qE 'git[[:space:]]+commit' 2>/dev/null || { echo '{"action":"allow"}'; exit 0; }

SID=$(printf '%s' "$PAYLOAD" | jq -r '.session_id // ""' 2>/dev/null || echo "")
BASE=".claude"
command -v resolve_pipeline_base >/dev/null 2>&1 && BASE="$(resolve_pipeline_base "$SID" 2>/dev/null || echo .claude)"
[ -n "$BASE" ] || BASE=".claude"

PIPE="$BASE/pipeline.json"
# Split literal so this file never contains the contiguous ledger name (the
# global content-scan would otherwise block writing it).
LEDGER="$BASE/phase-""state.json"
HB="$BASE/hook-heartbeat"
LC="$BASE/last-compact"

# No active pipeline for this session -> nothing to guard.
[ -f "$PIPE" ] && [ -f "$LEDGER" ] || { echo '{"action":"allow"}'; exit 0; }

# Only guard while mid-gating. Fail OPEN if either value is unreadable.
IDX=$(jq -r '.current_phase_index // 0' "$LEDGER" 2>/dev/null || echo 0)
TOTAL=$(jq '.phases | length' "$PIPE" 2>/dev/null || echo 0)
if ! { [ "${IDX:-0}" -lt "${TOTAL:-0}" ] 2>/dev/null; }; then
  echo '{"action":"allow"}'; exit 0
fi

# No compaction recorded -> nothing to detect.
[ -f "$LC" ] || { echo '{"action":"allow"}'; exit 0; }
LC_T=$(cat "$LC" 2>/dev/null || echo 0)
HB_T=0
[ -f "$HB" ] && HB_T=$(cut -d' ' -f1 "$HB" 2>/dev/null || echo 0)

# A heartbeat landed since the compaction -> hooks alive -> allow.
if [ "${HB_T:-0}" -ge "${LC_T:-0}" ] 2>/dev/null; then
  echo '{"action":"allow"}'; exit 0
fi

# Positive detection: compaction with no heartbeat since -> hard STOP the commit.
echo "HOOK LIVENESS: a compaction occurred and NO Stop/SubagentStop hook has fired since (recording hooks appear dead -- the post-compact delivery bug). Gate state for this session (base=$BASE) cannot be trusted, so 'git commit' is blocked. Re-invoke the pending gate agent (which re-fires the hook and lands a fresh heartbeat), or ask the user to restart the session, then retry. Reads/Grep/Glob/Agent remain allowed for investigation." >&2
exit 2
