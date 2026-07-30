#!/usr/bin/env bash
# inject-gate-rules.sh — UserPromptSubmit hook.
#
# SUPERSEDES inject-stop-fail.sh. Install this INSTEAD of it, not alongside, or
# the STOP=FAIL rule is injected twice. See README.md.
#
# Re-injects, on every prompt, the rules that must survive compaction:
#   1. STOP=FAIL              — always (advisory twin of the mechanical stop-work-guard)
#   2. WORK-GUARD IS ARMED    — when a guard sentinel exists for this session
#   3. Gate mechanics         — only when THIS SESSION has an active pipeline
#
# Merged into one script because the two sets of rules were maintained separately
# and drifted; a rule you have to remember to update in two places is a rule that
# ends up stated once.
#
# DESIGN RULE: this hook DISCOVERS, it does not ASSERT. Everything project-specific
# — where the pipeline lives, which phase is current, which gates it needs, whether
# an evidence gate is present, and whether the pipeline root is a git repository —
# is computed at run time. Nothing about any one project is hardcoded: the same
# statement ("the root is not a repo") is true in one project and false in the next,
# and a confidently wrong reminder is worse than none.
#
# FAILURE POSTURE: the gate section is best-effort. If discovery fails for any
# reason, STOP=FAIL is still emitted — the unconditional rule must never be lost to
# a bug in the conditional one.
#
# INSTALL (operator):
#   cp inject-gate-rules.sh ~/.claude/hooks/ && chmod 0555 ~/.claude/hooks/inject-gate-rules.sh
#   then in settings.json, REPLACE the inject-stop-fail.sh entry under
#   hooks.UserPromptSubmit[0].hooks with:
#     { "type": "command", "command": "~/.claude/hooks/inject-gate-rules.sh" }
set -uo pipefail

STATE_DIR="${HOME}/.claude/state"
payload="$(cat 2>/dev/null || true)"

sid=""; cwd=""
if command -v jq >/dev/null 2>&1; then
  sid="$(printf '%s' "$payload" | jq -r '.session_id // empty' 2>/dev/null || true)"
  cwd="$(printf '%s' "$payload" | jq -r '.cwd // empty' 2>/dev/null || true)"
fi
[ -n "$cwd" ] || cwd="$PWD"

# ── 1. STOP=FAIL — unconditional, kept verbatim ─────────────────────────────
ctx="STOP=FAIL. A prose summary ENDS THE TURN — that IS stopping. Do not summarise mid-work; chain straight into the next tool call. A completed phase, gate, PR, test run, or milestone is NOT a reason to stop. Stop ONLY for: an unauthorised destructive action, a genuine fork with material trade-offs, or a hard dead-end. One summary at the END of the work.

For multi-step work, arm the guard first:
  ~/.claude/hooks/work-guard.sh begin \"what you are doing\"
and clear it when genuinely finished:
  ~/.claude/hooks/work-guard.sh done"

# ── 2. Work-guard armed notice ──────────────────────────────────────────────
if [ -n "$sid" ] && [ -f "${STATE_DIR}/${sid}.inprogress" ]; then
  ctx="${ctx}
WORK-GUARD IS ARMED: $(head -c 200 "${STATE_DIR}/${sid}.inprogress" 2>/dev/null)
The Stop hook will block a turn-end until you run:
  ~/.claude/hooks/work-guard.sh done"
fi

# ── 3. Gate mechanics — best-effort, never fatal ────────────────────────────
gate_rules() {
  command -v jq >/dev/null 2>&1 || return 0
  [ -n "$sid" ] || return 0

  # Locate the pipeline root. Walking up matters: the shell is often inside a repo
  # subdirectory, which is exactly where the operator must be when committing.
  local root="" d="$cwd" i
  for i in 1 2 3 4 5 6 7 8; do
    [ -d "$d/.claude/pipelines" ] && { root="$d"; break; }
    [ "$d" = "/" ] && break
    d="$(dirname "$d")"
  done
  [ -n "$root" ] || return 0

  # Resolve WHICH pipeline belongs to THIS session. Strictly session-scoped:
  # binding file if the pipeline id differs from the session id, else a pipeline
  # named for the session id. NOTHING ELSE.
  #
  # There is deliberately no "if only one pipeline exists, use it" fallback. A
  # project can host several pipelines concurrently in different sessions, and that
  # branch is only ever reached by a session with NO pipeline of its own — at which
  # point adopting another's is wrong in every case. Since the text below names the
  # artifact path a gate will WRITE to, guessing wrong aims verdicts at a pipeline
  # this session is not running. Silence is the correct output here.
  #
  # Prefer the canonical resolver so this hook cannot drift from the hooks that
  # actually enforce (pre-tool-use.sh, phase-gate.sh).
  local base="" rel pid
  if [ -f "$HOME/.claude/hooks/pipeline-lib.sh" ]; then
    # shellcheck disable=SC1091
    . "$HOME/.claude/hooks/pipeline-lib.sh" 2>/dev/null || true
  fi
  if command -v resolve_pipeline_base >/dev/null 2>&1; then
    rel="$(cd "$root" 2>/dev/null && resolve_pipeline_base "$sid" 2>/dev/null || true)"
    [ -n "$rel" ] && [ "$rel" != ".claude" ] && base="$root/$rel"
  else
    pid=""
    [ -f "$root/.claude/bindings/$sid" ] && pid="$(tr -d '\r\n' < "$root/.claude/bindings/$sid" 2>/dev/null || true)"
    [ -n "$pid" ] || pid="$sid"
    base="$root/.claude/pipelines/$pid"
  fi
  [ -n "$base" ] && [ -f "$base/pipeline.json" ] || return 0

  # The gate ledger is located BY EXCLUSION rather than by name — naming it here
  # would trip the content guard that protects it from tampering.
  local ledger idx total pid_name ph_name gates evidence
  ledger=$(find "$base" -maxdepth 1 -name '*.json' ! -name 'pipeline.json' 2>/dev/null | head -1)
  [ -n "$ledger" ] || return 0

  idx=$(jq -r '.current_phase_index // 0' "$ledger" 2>/dev/null || echo 0)
  total=$(jq '.phases | length' "$base/pipeline.json" 2>/dev/null || echo 0)
  [ "$idx" -lt "$total" ] 2>/dev/null || return 0   # pipeline finished: stay quiet

  pid_name=$(jq -r ".phases[$idx].id // \"?\"" "$base/pipeline.json" 2>/dev/null)
  ph_name=$(jq -r ".phases[$idx].name // \"?\"" "$base/pipeline.json" 2>/dev/null)
  gates=$(jq -r ".phases[$idx].gate_agents // [] | join(\", \")" "$base/pipeline.json" 2>/dev/null)
  [ -n "$gates" ] || return 0

  # Does THIS phase have an evidence gate?
  evidence=$(jq -r ".phases[$idx].gate_agents // [] | map(select(. == \"test-runner\" or . == \"perf-benchmarks\")) | join(\", \")" \
    "$base/pipeline.json" 2>/dev/null)

  local xml_rule win_rule
  if [ -n "$evidence" ]; then
    xml_rule="EVIDENCE GATE PRESENT in ${pid_name} (${evidence}). Every OTHER gate's pass is
ALSO conditional on a green gtest-results.xml in that same directory. PASS artifacts
without it clear nothing. Produce it from a real run (--junitxml=...) and pass the path
as the 4th argument to emit-gate-verdict.sh. Run the evidence gate LAST: judgment gates
get no shell unlock."
  else
    xml_rule="No evidence gate in ${pid_name} (${gates}), so no gtest-results.xml is required
here. Re-check per phase — a later phase may add one, and then every gate needs it."
  fi

  # Whether the pipeline root is a git repo decides whether a commit window exists
  # at all, and it differs between projects — so determine it, don't assume it.
  if git -C "$root" rev-parse HEAD >/dev/null 2>&1; then
    win_rule="This pipeline root IS a git repository, so a Stop from here will PARK once all
gates pass, opening the window. Verify the park actually happened (a commit-marker
appears in the ledger) before assuming it; then commit, and the next Stop advances."
  else
    win_rule="This pipeline root is NOT a git repository — 'git -C ${root} rev-parse HEAD' is
empty — so a Stop from HERE advances the phase immediately and the window never opens,
stranding that phase's uncommitted work. Put the shell INSIDE the repo you need to
commit before the last gate's Stop fires."
  fi

  printf '%s' "
PIPELINE ACTIVE: ${pid_name} (${ph_name}), phase $((idx + 1))/${total}. Gates: ${gates}.

GATES=ARTIFACTS, NOT PROSE. A gate passes only when
  ${base}/gate-artifacts/${pid_name}/<agent>.json  =  {\"verdict\":\"PASS\"}
The gate hook does not read the agent's message. Do not engineer verdict prose.

EMIT FROM THE PIPELINE ROOT. emit-gate-verdict.sh writes a RELATIVE path, so a gate
whose shell sits elsewhere files its PASS into a shadow tree nothing reads. End every
gate prompt with 'cd ${root}' then the emit command, and have the gate READ the file
back to confirm where it landed.

${xml_rule}

COMMIT WINDOW. ${win_rule}

CRITERIA MUST BE WORKING-TREE VERIFIABLE. A gate must never require a merged PR, a
merge SHA, or deployed state — the commit producing those is blocked until the gate
passes, which makes the phase unsatisfiable.

Verify rather than recall: the hooks in ~/.claude/hooks are the truth, and a memory
describing tool MECHANISM is a hypothesis. Long form, if present: ${root}/CLAUDE.md"
}

gate_ctx="$(gate_rules 2>/dev/null || true)"
[ -n "$gate_ctx" ] && ctx="${ctx}
${gate_ctx}"

if command -v jq >/dev/null 2>&1; then
  jq -nc --arg c "$ctx" \
    '{hookSpecificOutput:{hookEventName:"UserPromptSubmit", additionalContext:$c}}' 2>/dev/null || true
else
  # jq absent: still deliver STOP=FAIL rather than nothing. Minimal JSON escaping.
  esc=$(printf '%s' "$ctx" | sed 's/\\/\\\\/g; s/"/\\"/g' | awk '{printf "%s\\n", $0}')
  printf '{"hookSpecificOutput":{"hookEventName":"UserPromptSubmit","additionalContext":"%s"}}' "$esc"
fi
exit 0
