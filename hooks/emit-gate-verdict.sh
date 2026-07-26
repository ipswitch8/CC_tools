#!/bin/bash
# emit-gate-verdict.sh <phase-id> <agent> <PASS|FAIL> [gtest-xml] [target-test] [--pipeline <pid>]
#
# GENERIC deterministic gate-artifact writer. Run this as a gate agent's FINAL
# action so the gate hook consumes a byte-correct verdict artifact instead of
# parsing free prose. Writes under the pipeline's base:
#   <base>/gate-artifacts/<phase-id>/<agent>.json   ({verdict,target})
#   <base>/gate-artifacts/<phase-id>/gtest-results.xml   (if an xml is given)
# where <base> is .claude/pipelines/<pid> when --pipeline is supplied, else the
# legacy .claude (single-pipeline). The gate agent passes --pipeline <pid> for a
# namespaced pipeline (the id it was told to gate).
set -uo pipefail

ARGS=(); PID=""
while [ $# -gt 0 ]; do
  case "$1" in
    --pipeline) PID="${2:-}"; shift 2 ;;
    *) ARGS+=("$1"); shift ;;
  esac
done
set -- "${ARGS[@]:-}"

PHASE="${1:-}"; AGENT="${2:-}"; VERDICT="${3:-}"; XML="${4:-}"; TARGET="${5:-}"
if [ -z "$PHASE" ] || [ -z "$AGENT" ] || [ -z "$VERDICT" ]; then
  echo "usage: emit-gate-verdict.sh <phase-id> <agent> <PASS|FAIL> [gtest-xml] [target-test] [--pipeline <pid>]" >&2
  exit 2
fi
case "$VERDICT" in PASS|FAIL) ;; *) echo "verdict must be PASS or FAIL, got: '$VERDICT'" >&2; exit 2 ;; esac

if [ -n "$PID" ]; then BASE=".claude/pipelines/${PID}"; else BASE=".claude"; fi
DIR="${BASE}/gate-artifacts/${PHASE}"
mkdir -p "$DIR" || { echo "cannot create $DIR" >&2; exit 2; }

if ! printf '{"verdict":"%s","target":"%s"}\n' "$VERDICT" "$TARGET" | jq . > "${DIR}/${AGENT}.json" 2>/dev/null; then
  printf '{"verdict":"%s","target":"%s"}\n' "$VERDICT" "$TARGET" > "${DIR}/${AGENT}.json"
fi

if [ -n "$XML" ]; then
  if [ -f "$XML" ]; then cp -f "$XML" "${DIR}/gtest-results.xml"
  else echo "warning: gtest xml not found: $XML (evidence gate will fail closed)" >&2; fi
fi

echo "wrote ${DIR}/${AGENT}.json (verdict=${VERDICT}${PID:+, pipeline=${PID}}${XML:+, xml copied})"
exit 0
