#!/bin/bash
# pipeline-ctl.sh — manage per-session multi-pipeline state (GENERIC).
#
# Universal: main folder only, no git, no worktree. Commands:
#   init <session-id> <pipeline-id>      create the namespaced state dir + bind session
#   bind|reattach <session-id> <pipeline-id>   (re)bind a session to a pipeline
#   resolve <session-id>                 print the base dir this session resolves to
#   status <session-id>                  show binding + base + whether state exists
#   list                                 list all pipelines and their session bindings
#
# The agent knows its own pipeline-id (from context) and reads its session-id
# from its scratchpad/transcript path, then calls `init` / `reattach` directly.
set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/pipeline-lib.sh"

cmd="${1:-}"; shift || true
case "$cmd" in
  init)
    sid="${1:-}"; pid="${2:-$sid}"
    [ -n "$sid" ] || { echo "usage: pipeline-ctl.sh init <session-id> [pipeline-id]  (pipeline-id defaults to session-id)" >&2; exit 2; }
    mkdir -p ".claude/pipelines/${pid}/gate-artifacts" || exit 2
    # A binding is only needed when the pipeline id differs from the session id;
    # by default the resolver maps <session-id> -> .claude/pipelines/<session-id>/.
    if [ "$pid" != "$sid" ]; then bind_session "$sid" "$pid" || exit 2; fi
    echo "initialized .claude/pipelines/${pid}  (session ${sid}$([ "$pid" = "$sid" ] && echo ', default id' || echo ', bound'))"
    echo "next: have planner write pipeline.json + the ledger INTO .claude/pipelines/${pid}/"
    ;;
  bind|reattach)
    bind_session "${1:-}" "${2:-}" && echo "bound ${1:-} -> ${2:-}"
    ;;
  resolve)
    resolve_pipeline_base "${1:-}"; echo
    ;;
  status)
    sid="${1:-}"; b="$(session_binding "$sid")"; base="$(resolve_pipeline_base "$sid")"
    echo "session:  ${sid:-<none>}"
    echo "binding:  ${b:-<none> (legacy)}"
    echo "base:     ${base}"
    echo "pipeline.json present: $([ -f "$base/pipeline.json" ] && echo yes || echo no)"
    ;;
  list)
    echo "pipelines:"
    ls -1 .claude/pipelines 2>/dev/null | sed 's/^/  /' || echo "  (none)"
    echo "bindings:"
    if [ -d .claude/bindings ]; then
      for f in .claude/bindings/*; do [ -f "$f" ] && echo "  $(basename "$f") -> $(tr -d '[:space:]' < "$f")"; done
    else echo "  (none)"; fi
    ;;
  *)
    echo "usage: pipeline-ctl.sh {init|bind|reattach|resolve|status|list} ..." >&2; exit 2
    ;;
esac
