#!/usr/bin/env bash
# stop-work-guard.sh — mechanical enforcement of STOP=FAIL.
#
# WHY THIS EXISTS
# ---------------
# CLAUDE.md says STOP=FAIL. Memory files and CLAUDE.md are ADVISORY: text the
# model may or may not act on. The observed failure mode is that ending a turn
# does not FEEL like an action, so the rule is never consulted at the moment it
# applies. Advisory text cannot catch an unnoticed decision — it was restated
# five-plus times in one session and still failed.
#
# This hook fires at the exact moment of the failure (Stop) and BLOCKS it,
# feeding the reason back so work continues. Same philosophy as
# hook-liveness-guard.sh: enforce mechanically, fail closed.
#
# CONTRACT
#   sentinel present -> block the stop, tell the model to continue
#   sentinel absent  -> allow (ordinary conversational turns unaffected)
#
# The model arms/disarms it around multi-step work:
#   ~/.claude/hooks/work-guard.sh begin "short description"
#   ~/.claude/hooks/work-guard.sh done
#
# SAFETY VALVES (must never wedge a session)
#   1. stop_hook_active -> allow immediately. Claude Code sets this when it is
#      already re-entering from a Stop hook; honouring it prevents an infinite
#      block loop.
#   2. MAX_BLOCKS consecutive blocks -> allow, clear the sentinel, explain. A
#      stale sentinel costs a few extra turns, never a hung session.
#   3. Any internal error -> allow. This hook must never be why a session
#      cannot end.
set -uo pipefail

STATE_DIR="${HOME}/.claude/state"
MAX_BLOCKS="${WORK_GUARD_MAX_BLOCKS:-4}"

allow() { exit 0; }

payload="$(cat 2>/dev/null || true)"

# Safety valve 1
if printf '%s' "$payload" | jq -e '.stop_hook_active == true' >/dev/null 2>&1; then
  allow
fi

session="$(printf '%s' "$payload" | jq -r '.session_id // empty' 2>/dev/null || true)"
[ -n "$session" ] || allow

sentinel="${STATE_DIR}/${session}.inprogress"
counter="${STATE_DIR}/${session}.blockcount"

if [ ! -f "$sentinel" ]; then
  rm -f "$counter" 2>/dev/null
  allow
fi

n=0
[ -f "$counter" ] && n="$(cat "$counter" 2>/dev/null || echo 0)"
case "$n" in ''|*[!0-9]*) n=0 ;; esac
n=$((n + 1))

# Safety valve 2
if [ "$n" -gt "$MAX_BLOCKS" ]; then
  rm -f "$sentinel" "$counter" 2>/dev/null
  jq -nc --arg m "work-guard: auto-released after ${MAX_BLOCKS} blocks; sentinel cleared (likely stale). If work IS still outstanding, re-arm: ~/.claude/hooks/work-guard.sh begin" \
    '{systemMessage:$m}' 2>/dev/null
  allow
fi

printf '%s' "$n" > "$counter" 2>/dev/null

task="$(head -c 400 "$sentinel" 2>/dev/null)"
[ -n "$task" ] || task="(no description recorded)"

reason="STOP=FAIL — you are ending the turn while multi-step work is still marked IN PROGRESS.

In progress: ${task}

A prose summary ENDS THE TURN, which IS stopping. Do not summarise mid-work.
Chain straight into the next tool call and keep going until the work is done.

Legitimate stops are ONLY:
  (a) an unauthorised destructive/irreversible action,
  (b) a genuine fork with material trade-offs,
  (c) a hard technical dead-end.
A completed phase, gate, PR, test run, or milestone is NOT one of them.

If the work IS genuinely finished, clear the marker first:
    ~/.claude/hooks/work-guard.sh done
then end the turn. (Block ${n}/${MAX_BLOCKS} — auto-releases after that.)"

jq -nc --arg r "$reason" '{decision:"block", reason:$r}' 2>/dev/null || allow
exit 0
