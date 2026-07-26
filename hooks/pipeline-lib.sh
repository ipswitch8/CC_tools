#!/bin/bash
# pipeline-lib.sh — per-session multi-pipeline state resolver + binding.
#
# GENERIC (not project-specific). Meant to live beside the hooks (project
# .claude/hooks/ or global ~/.claude/hooks/) so hooks can source it from their
# own directory:  . "$(dirname "${BASH_SOURCE[0]}")/pipeline-lib.sh"
#
# Universal model: a session always runs in the MAIN project folder (no git, no
# worktree). The only per-session signal a hook has to tell concurrent
# same-folder sessions apart is the payload session_id, which is STABLE across
# compaction and --resume. Each session binds to one pipeline; state is
# namespaced per pipeline. State paths are resolved relative to the CURRENT
# WORKING DIRECTORY (the project root), independent of where this file lives.
#
# UNIFORM: single and concurrent pipelines are handled the same way — every
# pipeline is namespaced, keyed by session id. The pipeline id DEFAULTS to the
# session id, so a lone pipeline needs no special handling. Legacy singular
# ".claude" is returned only as a last resort when a hook fires with no session id.
#
# Layout (relative to project cwd):
#   .claude/pipelines/<pipeline-id>/   holds pipeline.json, the gate ledger, gate-artifacts/
#                                      (<pipeline-id> defaults to <session-id>)
#   .claude/bindings/<session-id>      contents are <pipeline-id>; present only when
#                                      the id differs from the session id (reattach)

# <session-id> -> <pipeline-id> (empty string if unbound)
session_binding() {
  local sid="${1:-}"
  [ -n "$sid" ] || return 0
  local f=".claude/bindings/${sid}"
  [ -f "$f" ] && tr -d '[:space:]' < "$f" 2>/dev/null || true
}

# <session-id> -> base dir for this session's pipeline state.
# UNIFORM: single and concurrent pipelines are namespaced identically. The
# pipeline id DEFAULTS to the session id, so a lone pipeline lives in
# .claude/pipelines/<session-id>/ — there is no separate legacy path for a
# single pipeline. An explicit binding (reattach / a named pipeline) overrides
# the default. Legacy singular ".claude" is returned only as a last resort when
# there is no session id at all.
resolve_pipeline_base() {
  local sid="${1:-}" pid
  [ -n "$sid" ] || { printf '%s' ".claude"; return 0; }
  pid="$(session_binding "$sid")"
  [ -n "$pid" ] || pid="$sid"
  printf '%s' ".claude/pipelines/${pid}"
}

# bind a session to a pipeline (idempotent, overwrites any prior binding).
bind_session() {
  local sid="${1:-}" pid="${2:-}"
  [ -n "$sid" ] && [ -n "$pid" ] || { echo "bind_session: need <session-id> <pipeline-id>" >&2; return 2; }
  mkdir -p ".claude/bindings" 2>/dev/null || return 2
  printf '%s\n' "$pid" > ".claude/bindings/${sid}"
}
