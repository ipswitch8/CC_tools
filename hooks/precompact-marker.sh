#!/bin/bash
# precompact-marker.sh — record that a compaction is occurring (GENERIC).
#
# Fires on PreCompact (a distinct event from Stop/SubagentStop, so it still fires
# as part of compaction). Writes <base>/last-compact (epoch) so the liveness
# check can tell whether any Stop/SubagentStop heartbeat has landed SINCE the
# compaction — if none has, the recording hooks are presumed dead. Also snapshots
# the durable pipeline evidence (plan + which gate artifacts exist) so position
# can be re-grounded from ground truth after the conversation is compacted.
# <base> is this session's resolved pipeline base (namespaced if bound, else
# legacy). Never references the gate ledger file. Best-effort; never blocks.
set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/pipeline-lib.sh" 2>/dev/null || true

PAYLOAD=$(cat 2>/dev/null || true)
SID=$(printf '%s' "$PAYLOAD" | jq -r '.session_id // ""' 2>/dev/null || echo "")

BASE=".claude"
command -v resolve_pipeline_base >/dev/null 2>&1 && BASE="$(resolve_pipeline_base "$SID" 2>/dev/null || echo .claude)"
[ -n "$BASE" ] || BASE=".claude"

# Only mark a compaction where a pipeline actually exists (no clutter otherwise).
[ -f "$BASE/pipeline.json" ] || exit 0

date +%s > "$BASE/last-compact" 2>/dev/null || true

{
  echo "compact_at=$(date -Iseconds 2>/dev/null || date 2>/dev/null || echo unknown)"
  echo "session=${SID:-unknown}"
  echo "base=$BASE"
  if [ -f "$BASE/pipeline.json" ]; then
    echo "task=$(jq -r '.task // ""' "$BASE/pipeline.json" 2>/dev/null | head -c 400)"
    echo "phases=$(jq -r '[.phases[].id] | join(",")' "$BASE/pipeline.json" 2>/dev/null)"
  fi
  echo "gate_artifacts_present:"
  if [ -d "$BASE/gate-artifacts" ]; then
    ( cd "$BASE/gate-artifacts" && find . -name '*.json' -o -name '*.xml' 2>/dev/null | sed 's/^/  /' )
  else
    echo "  (none)"
  fi
} > "$BASE/pipeline-status.snapshot" 2>/dev/null || true

exit 0
