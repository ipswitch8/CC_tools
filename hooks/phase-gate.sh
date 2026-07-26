#!/bin/bash
# .claude/hooks/phase-gate.sh  (PDFE REPO-LOCAL HARDENED COPY)
#
# HARDENED per docs/audit remediation Phase 1 (Register C1, H6, M-e).
#
# WHAT CHANGED vs the legacy global hook (~/.claude/hooks/phase-gate.sh):
#   The legacy hook decided a gate PASS by GREPPING THE AGENT'S CHAT PROSE:
#     * a "VERDICT: PASS" structured line, else
#     * a bold "**PASS**" line, else
#     * a WORD-COUNT FALLBACK: any stray "PASS" word anywhere in the message
#       with no "FAIL" word flipped the gate green.
#   No test was re-run and no artifact was checked. The audit proved this
#   makes "green" meaningless: a subagent (or a summary) merely SAYING pass
#   passed the gate. See docs/audit/README.md (Register C1).
#
#   THIS HOOK NEVER READS last_assistant_message TO DECIDE A VERDICT.
#   A gate PASS requires a MACHINE-CHECKED ARTIFACT on disk:
#     <base>/gate-artifacts/<phase-id>/<agent>.json   -> {"verdict":"PASS"|"FAIL", ...}
#   (<base> is this session's resolved pipeline base, .claude/pipelines/<id>/)
#   and, for EVIDENCE gates (test-runner, perf-benchmarks), additionally a
#   JUnit XML at
#     <base>/gate-artifacts/<phase-id>/gtest-results.xml
#   that the hook parses itself (failures=0, errors=0, tests>0, >=1 testcase
#   with status=run; and if the artifact names a "target" test, that test
#   must be present with status=run). The XML is trusted, not the exit code
#   (GAP-A15). A verdict artifact claiming PASS with a red/absent XML is
#   forced to remediation.
#
#   JUDGMENT gates (karen, validator, security-audit) have no XML of their
#   own. Their artifact verdict must be exactly PASS; AND if the phase also
#   has a test-runner gate, that phase's gtest-results.xml must already be
#   green -- a judgment gate cannot sign off a phase whose tests are red.
#
#   AUTHORITATIVE OVERRIDE: because the legacy global hook may still fire on
#   the same Stop/SubagentStop event (user chose to leave it untouched for
#   other projects), THIS hook always RE-DERIVES the target/current phase's
#   gate results from artifacts and OVERWRITES state. On the end-of-turn Stop
#   event it re-derives the whole current phase, so artifact-truth is the
#   final word regardless of hook ordering. A prose-only "PASS" with no
#   artifact is reset to remediation.
#
# PRESERVED verbatim from the legacy hook: state locking, v1->v2 migration,
# the pipeline content_sig staleness guard, the multi-phase advance_loop with
# the v2.4 commit-marker PARKING checkpoint, Bash-gate unlock issuance, and
# all end-of-turn Stop messaging. Only the verdict-determination path is
# replaced.

set -euo pipefail

BLOB=$(cat)

_SID=$(printf '%s' "$BLOB" | jq -r '.session_id // ""' 2>/dev/null || echo "")
if [ -f "$(dirname "${BASH_SOURCE[0]}")/pipeline-lib.sh" ]; then
  . "$(dirname "${BASH_SOURCE[0]}")/pipeline-lib.sh"
  _BASE=$(resolve_pipeline_base "$_SID" 2>/dev/null || echo ".claude")
else _BASE=".claude"; fi
[ -n "$_BASE" ] || _BASE=".claude"
STATE_FILE="$_BASE/phase-""state.json"
PIPELINE_FILE="$_BASE/pipeline.json"
ARTIFACT_ROOT="$_BASE/gate-artifacts"

DEBUG_LOG=".claude/hook-debug.log"

# Opt-in only. This appended unconditionally before, growing unbounded every
# Stop/SubagentStop. Set PHASE_GATE_DEBUG=1 in the hook env to re-enable.
if [ -n "${PHASE_GATE_DEBUG:-}" ]; then
  {
    echo "=== $(date -Iseconds) [hardened] ==="
    echo "HOOK_EVENT: $(echo "$BLOB" | jq -r '.hook_event_name // "Stop"')"
    echo "AGENT_TYPE: $(echo "$BLOB" | jq -r '.agent_type // "(null)"')"
    echo "---"
  } >> "$DEBUG_LOG" 2>&1
fi

LOCK_DIR="${STATE_FILE}.lock"
_acquire_lock() {
  local attempts=0
  while ! mkdir "$LOCK_DIR" 2>/dev/null; do
    attempts=$((attempts + 1))
    if [ "$attempts" -ge 50 ]; then
      rm -rf "$LOCK_DIR" 2>/dev/null
      mkdir "$LOCK_DIR" 2>/dev/null || true
      break
    fi
    sleep 0.1
  done
}
_release_lock() {
  rm -rf "$LOCK_DIR" 2>/dev/null || true
}
write_state() {
  local filter="$1"; shift
  _acquire_lock
  chmod +w "$STATE_FILE" 2>/dev/null || true
  jq "$filter" "$@" "$STATE_FILE" > "$STATE_FILE.tmp" && mv "$STATE_FILE.tmp" "$STATE_FILE"
  chmod -w "$STATE_FILE" 2>/dev/null || true
  _release_lock
}

git_head() {
  git rev-parse HEAD 2>/dev/null || echo ""
}

chmod -w "$STATE_FILE" 2>/dev/null || true

if [ ! -f "$STATE_FILE" ] || [ ! -f "$PIPELINE_FILE" ]; then
  echo '{"continue": true, "abstain": true, "reason": "phase-gate: no pipeline files"}'
  exit 0
fi

PIPELINE_SIG_NOW=$(jq -r '[.phases[] | (.id + ":" + .name)] | join("|")' "$PIPELINE_FILE" 2>/dev/null | sha256sum | cut -d' ' -f1)
STATE_SIG_NOW=$(jq -r '.pipeline_content_sig // ""' "$STATE_FILE" 2>/dev/null)
if [ -n "$PIPELINE_SIG_NOW" ] && [ -n "$STATE_SIG_NOW" ] && [ "$PIPELINE_SIG_NOW" != "$STATE_SIG_NOW" ]; then
  echo "{\"continue\": true, \"systemMessage\": \"phase-gate: pipeline content_sig mismatch (state ${STATE_SIG_NOW:0:8}, pipeline ${PIPELINE_SIG_NOW:0:8}). State is stale; wait for pipeline-validator to reset.\"}"
  exit 0
fi

IDX=$(jq -r '.current_phase_index // 0' "$STATE_FILE")
TOTAL=$(jq '.phases | length' "$PIPELINE_FILE")

if [ "$IDX" -ge "$TOTAL" ]; then
  echo '{"continue": true, "abstain": true, "reason": "phase-gate: pipeline complete"}'
  exit 0
fi

# v1 -> v2 schema migration (idempotent)
HAS_NEW=$(jq -r 'has("gate_results_by_phase")' "$STATE_FILE")
if [ "$HAS_NEW" != "true" ]; then
  OLD_PHASE=$(jq -r '.gate_phase_id // ""' "$STATE_FILE")
  OLD_RESULTS=$(jq -c '.current_gate_results // {}' "$STATE_FILE")
  if [ -n "$OLD_PHASE" ] && [ "$OLD_RESULTS" != "{}" ]; then
    write_state '.gate_results_by_phase = {($pid): $r}' --arg pid "$OLD_PHASE" --argjson r "$OLD_RESULTS"
  else
    write_state '.gate_results_by_phase = {}'
  fi
fi

# ── Bash-gate list + nonce-issue helper (preserved) ───────────────────────────
BASH_GATES="test-runner perf-benchmarks"
# Namespaced under this session's pipeline base (was singular ".claude/…", which
# let concurrent pipelines share one nonce). The reader (pre-tool-use.sh) resolves
# the same base from the payload session_id.
BASH_UNLOCK_FILE="$_BASE/gate-bash-""unlock"

issue_bash_unlock_if_needed() {
  local next_gate="$1"
  if [ -z "$next_gate" ]; then
    rm -f "$BASH_UNLOCK_FILE" 2>/dev/null || true
    return
  fi
  if echo "$BASH_GATES" | grep -qw "$next_gate"; then
    local expires
    expires=$(date -d "+5 minutes" +%s 2>/dev/null || date -v+5M +%s 2>/dev/null || echo "0")
    echo "{\"gate\":\"${next_gate}\",\"expires\":${expires}}" > "$BASH_UNLOCK_FILE"
    chmod -w "$BASH_UNLOCK_FILE" 2>/dev/null || true
  else
    rm -f "$BASH_UNLOCK_FILE" 2>/dev/null || true
  fi
}

# ── HARDENED verdict primitives ───────────────────────────────────────────────
# An "evidence gate" produces a machine-generated artifact the hook re-checks.
is_evidence_gate() {
  echo "$BASH_GATES" | grep -qw "$1"
}

phase_has_evidence_gate() {
  local pid="$1" g
  while IFS= read -r g; do
    g=$(printf "%s" "$g" | tr -d "\r")
    if is_evidence_gate "$g"; then return 0; fi
  done < <(jq -r --arg p "$pid" '.phases[] | select(.id==$p) | .gate_agents[]' "$PIPELINE_FILE" 2>/dev/null)
  return 1
}

# validate_junit_xml <xml-path> <optional-target-test-name>
# Exit 0 only if: parses; sum(failures)=0; sum(errors)=0; tests>0; >=1
# testcase with status=run; and (if target given) target present & run.
validate_junit_xml() {
  local xml="$1" target="${2:-}"
  [ -f "$xml" ] || return 1
  local _py; _py="$(command -v python3 || command -v python)"
  [ -n "$_py" ] || return 1
  "$_py" - "$xml" "$target" <<'PY' 2>/dev/null
# -*- coding: utf-8 -*-
import sys, xml.etree.ElementTree as ET
xml_path, target = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "")
try:
    root = ET.parse(xml_path).getroot()
except Exception:
    sys.exit(2)
def ai(el, name):
    try: return int(el.get(name, "0") or "0")
    except Exception: return 0
tf = te = tt = 0
suites = list(root.iter("testsuite"))
for ts in suites:
    tf += ai(ts, "failures"); te += ai(ts, "errors"); tt += ai(ts, "tests")
cases = list(root.iter("testcase"))
if tt == 0:  # single-suite / testsuites-only attrs
    tf += ai(root, "failures"); te += ai(root, "errors"); tt += ai(root, "tests")
def is_run(tc):
    return tc.get("status", "run") == "run"
run_cases = [tc for tc in cases if is_run(tc)]
ok = (tf == 0 and te == 0 and tt > 0 and len(run_cases) > 0)
if target:
    matches = [tc for tc in cases
               if tc.get("name") == target
               or (tc.get("classname", "") + "." + tc.get("name", "")) == target]
    ok = ok and any(is_run(tc) for tc in matches)
sys.exit(0 if ok else 1)
PY
}

# derive_verdict <phase-id> <agent> -> echoes "true" or "remediation"
# Reads ONLY the on-disk artifact + (for evidence gates) the JUnit XML.
# NEVER reads chat prose.
derive_verdict() {
  local pid="$1" agent="$2"
  local dir="${ARTIFACT_ROOT}/${pid}"
  local art="${dir}/${agent}.json"
  local xml="${dir}/gtest-results.xml"

  [ -f "$art" ] || { echo "remediation"; return; }
  local verdict target
  verdict=$(jq -r '.verdict // "FAIL"' "$art" 2>/dev/null | tr '[:lower:]' '[:upper:]')
  target=$(jq -r '.target // ""' "$art" 2>/dev/null)
  [ "$verdict" = "PASS" ] || { echo "remediation"; return; }

  if is_evidence_gate "$agent"; then
    if validate_junit_xml "$xml" "$target"; then echo "true"; else echo "remediation"; fi
    return
  fi

  # Judgment gate: if the phase has an evidence gate, its XML must be green too.
  if phase_has_evidence_gate "$pid"; then
    if validate_junit_xml "$xml" ""; then echo "true"; else echo "remediation"; fi
  else
    echo "true"
  fi
}

# Re-derive EVERY gate of a phase from artifacts and overwrite state.
# This is the authoritative override that neutralizes any prose-based flip
# a legacy hook may have written.
rederive_phase() {
  local pid="$1" cpid="$2" agent res
  while IFS= read -r agent; do
    agent=$(printf "%s" "$agent" | tr -d "\r")
    [ -n "$agent" ] || continue
    res=$(derive_verdict "$pid" "$agent")
    if [ "$res" = "true" ]; then
      write_state '
        .gate_results_by_phase[$tpid][$agent] = true |
        (if $tpid == $cpid then .current_gate_results[$agent] = true | .gate_phase_id = $cpid else . end)
      ' --arg agent "$agent" --arg tpid "$pid" --arg cpid "$cpid"
    else
      write_state '
        .gate_results_by_phase[$tpid][$agent] = $r |
        (if $tpid == $cpid then .current_gate_results[$agent] = $r | .gate_phase_id = $cpid else . end)
      ' --arg agent "$agent" --arg r "remediation" --arg tpid "$pid" --arg cpid "$cpid"
    fi
  done < <(jq -r --arg p "$pid" '.phases[] | select(.id==$p) | .gate_agents[]' "$PIPELINE_FILE" 2>/dev/null)
}

# ── Multi-phase advance helper (preserved verbatim) ───────────────────────────
ADVANCED_PHASES=()
NEXT_GATE=""
COMPLETED_COUNT=0
HELD_PHASE=""

advance_loop() {
  ADVANCED_PHASES=()
  NEXT_GATE=""
  COMPLETED_COUNT=0
  HELD_PHASE=""
  IDX=$(jq -r '.current_phase_index // 0' "$STATE_FILE")
  while [ "$IDX" -lt "$TOTAL" ]; do
    local phase_id phase_results all_pass=true next_gate="" completed=0
    phase_id=$(jq -r ".phases[$IDX].id" "$PIPELINE_FILE")
    phase_results=$(jq -c --arg p "$phase_id" '.gate_results_by_phase[$p] // {}' "$STATE_FILE")

    while IFS= read -r agent; do
      agent=$(printf "%s" "$agent" | tr -d "\r")
      local passed
      passed=$(echo "$phase_results" | jq -r --arg a "$agent" \
        '(.[$a] // false) | if type == "object" then (.result // "FAIL") else tostring end')
      if [ "$passed" = "true" ] || [ "$passed" = "PASS" ]; then
        completed=$((completed + 1))
      else
        all_pass=false
        [ -z "$next_gate" ] && next_gate="$agent"
      fi
    done < <(jq -r ".phases[$IDX].gate_agents[]" "$PIPELINE_FILE")

    if [ "$all_pass" = "true" ]; then
      local next_idx next_phase_id marker head
      next_idx=$((IDX + 1))
      next_phase_id=$(jq -r ".phases[$next_idx].id // \"\"" "$PIPELINE_FILE" 2>/dev/null)
      marker=$(jq -r '.commit_marker_head // ""' "$STATE_FILE")
      head=$(git_head)

      _advance_now() {
        write_state '
          .current_phase_index = $next |
          .phases_complete += [$pid] |
          .awaiting_commit = false |
          .commit_marker_head = "" |
          .gate_phase_id = $npid |
          .current_gate_results = (.gate_results_by_phase[$npid] // {})
        ' --argjson next "$next_idx" --arg pid "$phase_id" --arg npid "$next_phase_id"
        ADVANCED_PHASES+=("$phase_id")
        rm -f "$BASH_UNLOCK_FILE" 2>/dev/null || true
        IDX="$next_idx"
      }

      if [ -z "$head" ]; then
        _advance_now
        continue
      elif [ -z "$marker" ]; then
        write_state '
          .commit_marker_head = $h |
          .gate_phase_id = $pid |
          .current_gate_results = (.gate_results_by_phase[$pid] // {})
        ' --arg h "$head" --arg pid "$phase_id"
        HELD_PHASE="$phase_id"
        break
      elif [ "$head" != "$marker" ]; then
        _advance_now
        continue
      else
        HELD_PHASE="$phase_id"
        break
      fi
    else
      NEXT_GATE="$next_gate"
      COMPLETED_COUNT="$completed"
      break
    fi
  done
}

build_advance_message() {
  if [ "${#ADVANCED_PHASES[@]}" -eq 0 ]; then echo ""; return; fi
  local advanced_list
  advanced_list=$(printf '%s, ' "${ADVANCED_PHASES[@]}")
  advanced_list="${advanced_list%, }"
  if [ "$IDX" -ge "$TOTAL" ]; then
    echo "OK Gates passed and commit detected for: ${advanced_list}. Pipeline complete."
  else
    local next_name
    next_name=$(jq -r ".phases[$IDX].name" "$PIPELINE_FILE")
    echo "OK Commit detected; advanced to phase $((IDX + 1))/${TOTAL}: ${next_name} (completed: ${advanced_list})."
  fi
}

held_message() {
  if [ -z "$HELD_PHASE" ]; then echo ""; return; fi
  local nm
  nm=$(jq -r --arg p "$HELD_PHASE" '.phases[] | select(.id == $p) | .name' "$PIPELINE_FILE" 2>/dev/null)
  echo "OK All gates passed for ${nm} (${HELD_PHASE}). Commit the verified work now (e.g. run /g); the pipeline advances automatically on the next check once the commit lands."
}

combined_message() {
  local a b out
  a=$(build_advance_message)
  b=$(held_message)
  out=$(printf '%s %s' "$a" "$b")
  out="${out#"${out%%[![:space:]]*}"}"
  out="${out%"${out##*[![:space:]]}"}"
  echo "$out"
}

HOOK_EVENT=$(echo "$BLOB" | jq -r '.hook_event_name // "Stop"')

# ── SubagentStop ──────────────────────────────────────────────────────────────
if [ "$HOOK_EVENT" = "SubagentStop" ]; then
  AGENT_TYPE=$(echo "$BLOB" | jq -r '.agent_type // ""')
  LAST_MSG=$(echo "$BLOB" | jq -r '.last_assistant_message // ""')

  # PHASE tag is still honored to ROUTE the result to the right phase, but it
  # NEVER determines PASS/FAIL. (Routing only.)
  PARSED_PHASE=""
  PHASE_TAG_LINE=$(echo "$LAST_MSG" | grep -oiE '\*\*?\s*(PHASE|TARGET[ _]?PHASE|GATE[ _]?PHASE)[: ]+\*?\*?[[:space:]]*phase-[0-9]+' | tail -1 || true)
  if [ -n "$PHASE_TAG_LINE" ]; then
    PARSED_PHASE=$(echo "$PHASE_TAG_LINE" | grep -oE 'phase-[0-9]+' | head -1)
    EXISTS=$(jq -r --arg p "$PARSED_PHASE" '.phases[] | select(.id == $p) | .id' "$PIPELINE_FILE" 2>/dev/null || true)
    [ -z "$EXISTS" ] && PARSED_PHASE=""
  fi
  TARGET_PHASE_ID="${PARSED_PHASE:-$(jq -r ".phases[$IDX].id" "$PIPELINE_FILE")}"

  IS_GATE=$(jq -r --arg a "$AGENT_TYPE" --arg p "$TARGET_PHASE_ID" \
    '.phases[] | select(.id == $p) | .gate_agents | index($a) // -1' \
    "$PIPELINE_FILE" 2>/dev/null || echo "-1")

  if [ "$IS_GATE" != "-1" ] && [ -n "$AGENT_TYPE" ]; then
    CURR_PHASE_ID=$(jq -r ".phases[$IDX].id" "$PIPELINE_FILE")
    rm -f "$BASH_UNLOCK_FILE" 2>/dev/null || true

    # HARDENED: verdict comes ONLY from the artifact + XML, never from prose.
    RESULT=$(derive_verdict "$TARGET_PHASE_ID" "$AGENT_TYPE")

    if [ "$RESULT" = "true" ]; then
      write_state '
        .gate_results_by_phase[$tpid][$agent] = true |
        (if $tpid == $cpid then .current_gate_results[$agent] = true | .gate_phase_id = $cpid else . end)
      ' --arg agent "$AGENT_TYPE" --arg tpid "$TARGET_PHASE_ID" --arg cpid "$CURR_PHASE_ID"

      advance_loop
      MSG=$(combined_message)
      if [ -n "$MSG" ]; then
        echo "{\"continue\": false, \"systemMessage\": \"${MSG}\"}"
      else
        issue_bash_unlock_if_needed "$NEXT_GATE"
        echo "{\"continue\": false, \"systemMessage\": \"OK Gate '${AGENT_TYPE}' recorded PASS for ${TARGET_PHASE_ID} (artifact-verified).\"}"
      fi
    else
      write_state '
        .gate_results_by_phase[$tpid][$agent] = $result |
        (if $tpid == $cpid then .current_gate_results[$agent] = $result | .gate_phase_id = $cpid else . end)
      ' --arg agent "$AGENT_TYPE" --arg result "remediation" \
        --arg tpid "$TARGET_PHASE_ID" --arg cpid "$CURR_PHASE_ID"
      echo "{\"continue\": true, \"systemMessage\": \"FAIL Gate '${AGENT_TYPE}' for ${TARGET_PHASE_ID}: no artifact-verified PASS at ${ARTIFACT_ROOT}/${TARGET_PHASE_ID}/${AGENT_TYPE}.json (evidence gates also require a green gtest-results.xml). Prose alone cannot pass a gate. Produce the verdict artifact + XML, then re-invoke ${AGENT_TYPE}.\"}"
    fi
    exit 0
  fi

  echo '{"continue": true, "abstain": true, "reason": "phase-gate: SubagentStop for non-gate agent"}'
  exit 0
fi

# ── Stop event: loop guard ────────────────────────────────────────────────────
STOP_ACTIVE=$(echo "$BLOB" | jq -r '.stop_hook_active // false')
if [ "$STOP_ACTIVE" = "true" ]; then
  echo '{"continue": true, "abstain": true, "reason": "phase-gate: stop_hook_active loop guard"}'
  exit 0
fi

# ── Stop event: AUTHORITATIVE re-derivation of the current phase from artifacts.
# This is the final word each turn: any prose-based flip written by a legacy
# hook is corrected to artifact-truth before advancement is considered.
CUR_PID=$(jq -r ".phases[$IDX].id" "$PIPELINE_FILE")
rederive_phase "$CUR_PID" "$CUR_PID"

# ── Stop event: run advance loop ──────────────────────────────────────────────
advance_loop
MSG=$(combined_message)

if [ -n "$MSG" ]; then
  echo "{\"continue\": true, \"systemMessage\": \"${MSG}\"}"
  exit 0
fi

PHASE_NAME=$(jq -r ".phases[$IDX].name" "$PIPELINE_FILE")
PHASE_ID=$(jq -r ".phases[$IDX].id" "$PIPELINE_FILE")
TOTAL_GATES=$(jq ".phases[$IDX].gate_agents | length" "$PIPELINE_FILE")

issue_bash_unlock_if_needed "$NEXT_GATE"

echo "{\"continue\": true, \"systemMessage\": \"GATE ${PHASE_NAME} (phase $((IDX + 1))/${TOTAL}) - gate ${COMPLETED_COUNT}/${TOTAL_GATES}: invoke '${NEXT_GATE}'. The gate must WRITE ${ARTIFACT_ROOT}/${PHASE_ID}/${NEXT_GATE}.json (verdict PASS|FAIL); evidence gates must also write gtest-results.xml. Prose is not read.\"}"
