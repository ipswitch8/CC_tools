#!/usr/bin/env bash
# work-guard.sh — arm/disarm the STOP=FAIL sentinel that stop-work-guard.sh reads.
#
#   work-guard.sh begin "short description of the multi-step work"
#   work-guard.sh done
#   work-guard.sh status
#
# Session id is taken from CLAUDE_SESSION_ID if set, else the newest transcript
# basename under ~/.claude/projects/<encoded-cwd>/ (the documented method).
set -uo pipefail

STATE_DIR="${HOME}/.claude/state"
mkdir -p "$STATE_DIR" 2>/dev/null

resolve_session() {
  if [ -n "${CLAUDE_SESSION_ID:-}" ]; then
    printf '%s' "$CLAUDE_SESSION_ID"; return
  fi
  local enc proj newest
  enc="$(pwd -P | sed 's#/#-#g')"
  proj="${HOME}/.claude/projects/${enc}"
  [ -d "$proj" ] || proj="$(ls -dt "${HOME}"/.claude/projects/*/ 2>/dev/null | head -1)"
  newest="$(ls -t "${proj%/}"/*.jsonl 2>/dev/null | head -1)"
  [ -n "$newest" ] && basename "$newest" .jsonl
}

sid="$(resolve_session)"
if [ -z "$sid" ]; then
  echo "work-guard: could not resolve session id" >&2
  exit 2
fi

sentinel="${STATE_DIR}/${sid}.inprogress"
counter="${STATE_DIR}/${sid}.blockcount"

case "${1:-}" in
  begin)
    shift
    desc="${*:-multi-step work}"
    printf '%s\n' "$desc" > "$sentinel"
    rm -f "$counter" 2>/dev/null
    echo "work-guard: ARMED for session ${sid}"
    echo "  in progress: ${desc}"
    echo "  the Stop hook will now block turn-ends until 'work-guard.sh done'"
    ;;
  done|end|clear)
    if [ -f "$sentinel" ]; then
      echo "work-guard: DISARMED (was: $(head -c 200 "$sentinel"))"
    else
      echo "work-guard: already disarmed"
    fi
    rm -f "$sentinel" "$counter" 2>/dev/null
    ;;
  status)
    if [ -f "$sentinel" ]; then
      echo "ARMED   session=${sid}"
      echo "  task : $(head -c 300 "$sentinel")"
      echo "  blocks so far: $(cat "$counter" 2>/dev/null || echo 0)"
    else
      echo "disarmed  session=${sid}"
    fi
    ;;
  *)
    echo "usage: work-guard.sh {begin \"desc\"|done|status}" >&2
    exit 2
    ;;
esac
