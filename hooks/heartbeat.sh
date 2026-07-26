#!/bin/bash
# heartbeat.sh — proof-of-life for Stop/SubagentStop hook delivery (GENERIC).
#
# Fires on Stop and SubagentStop. Writes a line
#   <epoch> <event> <session_id>
# to <base>/hook-heartbeat, where <base> is this session's resolved pipeline base
# (namespaced when the session is bound, else legacy .claude). If these events
# stop being delivered (the post-compact hook-delivery bug), the heartbeat stops
# advancing and check-hooks-alive.sh / hook-liveness-guard.sh detect it by
# comparing against <base>/last-compact. The session_id is the ACTUAL payload
# value — it confirms directly (no transcript-name inference) that --resume
# preserves the session id, and it is the key the session->pipeline binding
# routes on. Best-effort and silent: never affects the turn.
set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/pipeline-lib.sh" 2>/dev/null || true

PAYLOAD=$(cat 2>/dev/null || true)
EV=$(printf '%s' "$PAYLOAD" | jq -r '.hook_event_name // "Stop"' 2>/dev/null || echo "Stop")
SID=$(printf '%s' "$PAYLOAD" | jq -r '.session_id // ""' 2>/dev/null || echo "")

BASE=".claude"
command -v resolve_pipeline_base >/dev/null 2>&1 && BASE="$(resolve_pipeline_base "$SID" 2>/dev/null || echo .claude)"
[ -n "$BASE" ] || BASE=".claude"

# Only record liveness where a pipeline actually exists — avoids creating a
# per-session dir under .claude/pipelines/ for sessions doing no pipeline work.
[ -f "$BASE/pipeline.json" ] || exit 0

# Field 1 (epoch) is unchanged, so `cut -d' ' -f1` consumers stay compatible.
printf '%s %s %s\n' "$(date +%s)" "$EV" "${SID:-unknown}" > "$BASE/hook-heartbeat" 2>/dev/null || true

exit 0
