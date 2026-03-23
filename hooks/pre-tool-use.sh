#!/bin/bash
# .claude/hooks/pre-tool-use.sh
#
# Fires on PreToolUse for all tools.
# In bypassPermissions mode, intercepts internet-touching operations and
# forces a permission prompt while allowing all local operations through.
#
# Hook event: PreToolUse

set -euo pipefail

PAYLOAD=$(cat)
TOOL_NAME=$(echo "$PAYLOAD" | jq -r '.tool_name // ""')

# ── Gate enforcement: block Write/Edit if gates pending ───────────────────────
STATE_FILE=".claude/phase-state.json"
PIPELINE_FILE=".claude/pipeline.json"

if [ -f "$STATE_FILE" ] && [ -f "$PIPELINE_FILE" ]; then
  # Only enforce on tools that constitute phase work
  if [[ "$TOOL_NAME" == "Write" || "$TOOL_NAME" == "Edit" || \
        "$TOOL_NAME" == "MultiEdit" || "$TOOL_NAME" == "NotebookEdit" ]]; then

    IDX=$(jq -r '.current_phase_index // 0' "$STATE_FILE")
    TOTAL=$(jq '.phases | length' "$PIPELINE_FILE")

    if [ "$IDX" -lt "$TOTAL" ]; then
      GATE_RESULTS=$(jq -c '.current_gate_results // {}' "$STATE_FILE")
      AWAITING_COMMIT=$(jq -r '.awaiting_commit // false' "$STATE_FILE")

      # Block if waiting for /g commit
      if [ "$AWAITING_COMMIT" = "true" ]; then
        PHASE_NAME=$(jq -r ".phases[$IDX].name" "$PIPELINE_FILE")
        echo "Gate enforcement: All gates passed for ${PHASE_NAME} but /g has not run yet. Run /g to commit this phase before writing new code." >&2
        exit 2
      fi

      # Find first pending gate
      NEXT_GATE=""
      while IFS= read -r agent; do
        PASSED=$(echo "$GATE_RESULTS" | jq -r --arg a "$agent" '.[$a] // false')
        if [ "$PASSED" != "true" ]; then
          NEXT_GATE="$agent"
          break
        fi
      done < <(jq -r ".phases[$IDX].gate_agents[]" "$PIPELINE_FILE" 2>/dev/null)

      # Block if any gate is pending AND at least one gate has run
      # (i.e. phase work is done but gates are incomplete)
      ANY_RAN=$(echo "$GATE_RESULTS" | jq '[to_entries[]] | length > 0')
      if [ -n "$NEXT_GATE" ] && [ "$ANY_RAN" = "true" ]; then
        PHASE_NAME=$(jq -r ".phases[$IDX].name" "$PIPELINE_FILE")
        echo "Gate enforcement: Cannot write new code — pending gate agent '${NEXT_GATE}' has not run for ${PHASE_NAME}. Invoke ${NEXT_GATE} before continuing." >&2
        exit 2
      fi
    fi
  fi
fi
```

Exit code 2 sends the message directly to Claude as a tool error — it cannot proceed and sees exactly why.

---

## Why `ANY_RAN` gate

The block only activates once at least one gate has run. This prevents it from blocking the phase work itself (when `current_gate_results` is empty, Claude should be free to write). The sequence becomes:
```
current_gate_results = {}     → writing allowed  (phase work in progress)
karen = true, validator = ?   → writing BLOCKED  (gates started, not finished)
karen = true, validator = true → writing allowed  (all gates passed, /g pending)
awaiting_commit = true        → writing BLOCKED  (waiting for /g)
phase advances, gates reset   → writing allowed  (next phase begins)

# ── Internet-native tools ─────────────────────────────────────────────────────
INTERNET_TOOLS=(
  "WebFetch"
  "WebSearch"
  "mcp__puppeteer__puppeteer_navigate"
  "mcp__puppeteer__puppeteer_screenshot"
  "mcp__brave__search"
)

for tool in "${INTERNET_TOOLS[@]}"; do
  if [ "$TOOL_NAME" = "$tool" ]; then
    echo '{"action": "ask", "reason": "⚠️  Internet access required — approval needed."}'
    exit 0
  fi
done

# ── Bash commands that touch the network ─────────────────────────────────────
if [ "$TOOL_NAME" = "Bash" ]; then
  COMMAND=$(echo "$PAYLOAD" | jq -r '.tool_input.command // ""')

  INTERNET_PATTERNS=(
    "curl "    "wget "     " fetch "
    "npm install" "npm i " "npm ci" "npm update"
    "pip install" "pip3 install" "pip upgrade"
    "brew install" "brew upgrade"
    "apt install" "apt-get install" "apt-get upgrade"
    "yarn add" "yarn install"
    "pnpm add" "pnpm install"
    "cargo add" "cargo install"
    "go get" "go install"
    "gem install" "bundle install"
    "docker pull" "docker push"
    "git push" "git fetch" "git pull" "git clone"
    "gh release"
    "ssh " "scp "
  )

  for pattern in "${INTERNET_PATTERNS[@]}"; do
    if echo "$COMMAND" | grep -qF "$pattern"; then
      echo "{\"action\": \"ask\", \"reason\": \"⚠️  Network operation detected: \`${COMMAND:0:80}\` — approval needed.\"}"
      exit 0
    fi
  done
fi

# ── All other tool uses: allow silently ───────────────────────────────────────
echo '{"action": "allow"}'
exit 0
