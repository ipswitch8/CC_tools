#!/bin/bash
# check-hooks-alive.sh [session-id] — report whether Stop/SubagentStop delivery is alive.
#
# GENERIC. A dead hook emits nothing, so liveness is inferred from the ABSENCE of
# an expected tick. Per-session (namespaced) when a session-id is given and bound;
# else legacy singular. Reads, under the resolved base:
#   <base>/last-compact   epoch written by precompact-marker.sh on PreCompact
#   <base>/hook-heartbeat epoch written by heartbeat.sh on Stop/SubagentStop
#
# ALIVE (exit 0): no compaction recorded, OR a heartbeat landed at/after the last
#                 compaction. DEAD (exit 1): a compaction happened and NO heartbeat
#                 has landed since (post-compact hook-delivery bug). Caller hard-STOPs.
set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/pipeline-lib.sh"

SID="${1:-}"
BASE="$(resolve_pipeline_base "$SID")"; [ -n "$BASE" ] || BASE=".claude"
HB="${BASE}/hook-heartbeat"
LC="${BASE}/last-compact"

[ -f "$LC" ] || { echo "ALIVE: no compaction recorded (base=$BASE)"; exit 0; }
LC_T=$(cat "$LC" 2>/dev/null || echo 0)
HB_T=0
[ -f "$HB" ] && HB_T=$(cut -d' ' -f1 "$HB" 2>/dev/null || echo 0)

if [ "${HB_T:-0}" -ge "${LC_T:-0}" ] 2>/dev/null; then
  echo "ALIVE: heartbeat ($HB_T) at/after last compaction ($LC_T) (base=$BASE)"
  exit 0
fi

echo "DEAD: last compaction ($LC_T) with no Stop/SubagentStop heartbeat since ($HB_T) (base=$BASE)."
echo "      Recording hooks appear dead (post-compact delivery bug). Do NOT trust"
echo "      gate state. Re-invoke the pending gate agent (re-fires the hook) or ask"
echo "      the user to restart the session, then re-check."
exit 1
