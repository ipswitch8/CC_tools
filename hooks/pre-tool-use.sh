#!/bin/bash
# .claude/hooks/pre-tool-use.sh
#
# Fires on PreToolUse for all tools.
# In bypassPermissions mode, intercepts internet-touching operations and
# forces a permission prompt while allowing all local operations through.
#
# Hook event: PreToolUse

set -euo pipefail

# ── Require jq ────────────────────────────────────────────────────────────────
if ! command -v jq &>/dev/null; then
  echo "Gate enforcement requires jq — install it and ensure it is on PATH." >&2
  exit 2
fi

PAYLOAD=$(cat)
TOOL_NAME=$(echo "$PAYLOAD" | jq -r '.tool_name // ""')

# ── Gate enforcement: block Write/Edit if gates pending ───────────────────────
STATE_FILE=".claude/phase-state.json"
PIPELINE_FILE=".claude/pipeline.json"

if [ -f "$STATE_FILE" ] && [ -f "$PIPELINE_FILE" ]; then

  # ── HARD BLOCK: Write/Edit targeting OR containing phase-state references ────
  if [[ "$TOOL_NAME" == "Write" || "$TOOL_NAME" == "Edit" || "$TOOL_NAME" == "MultiEdit" ]]; then
    FILE_PATH=$(echo "$PAYLOAD" | jq -r '.tool_input.file_path // ""')
    if echo "$FILE_PATH" | grep -qF 'phase-state'; then
      echo "Gate enforcement: Cannot modify phase-state.json via ${TOOL_NAME} — all state mutations are managed
exclusively by the stop hook." >&2
      exit 2
    fi
    CONTENT=""
    if [ "$TOOL_NAME" = "Write" ]; then
      CONTENT=$(echo "$PAYLOAD" | jq -r '.tool_input.content // ""')
    elif [ "$TOOL_NAME" = "Edit" ]; then
      CONTENT=$(echo "$PAYLOAD" | jq -r '.tool_input.new_string // ""')
    elif [ "$TOOL_NAME" = "MultiEdit" ]; then
      CONTENT=$(echo "$PAYLOAD" | jq -r '[.tool_input.edits[]?.new_string // ""] | join("\n")')
    fi
    if echo "$CONTENT" | grep -qF 'phase-state'; then
      echo "Gate enforcement: File contents reference phase-state.json — this could be used to bypass gate
enforcement via script execution. All state mutations are managed exclusively by the stop hook." >&2
      exit 2
    fi
  fi

  # Enforce on ALL tools that constitute phase work
  if [[ "$TOOL_NAME" == "Write" || "$TOOL_NAME" == "Edit" || \
        "$TOOL_NAME" == "MultiEdit" || "$TOOL_NAME" == "NotebookEdit" || \
        "$TOOL_NAME" == "Bash" ]]; then

    IDX=$(jq -r '.current_phase_index // 0' "$STATE_FILE")
    TOTAL=$(jq '.phases | length' "$PIPELINE_FILE")

    if [ "$IDX" -lt "$TOTAL" ]; then
      GATE_RESULTS=$(jq -c '.current_gate_results // {}' "$STATE_FILE")
      AWAITING_COMMIT=$(jq -r '.awaiting_commit // false' "$STATE_FILE")

      # Block if waiting for /g commit
      if [ "$AWAITING_COMMIT" = "true" ]; then
        PHASE_NAME=$(jq -r ".phases[$IDX].name" "$PIPELINE_FILE")
        echo "Gate enforcement: All gates passed for ${PHASE_NAME} but /g has not run yet. Run /g to commit this
 phase before writing new code." >&2
        exit 2
      fi

      # Find first pending gate.
      NEXT_GATE=""
      while IFS= read -r agent; do
        agent=$(printf "%s" "$agent" | tr -d "\r")
        PASSED=$(echo "$GATE_RESULTS" | jq -r --arg a "$agent" '(.[$a] // false) | if type == "object" then
(.result // "FAIL") else tostring end')
        if [ "$PASSED" != "true" ] && [ "$PASSED" != "PASS" ]; then
          NEXT_GATE="$agent"
          break
        fi
      done < <(jq -r ".phases[$IDX].gate_agents[]" "$PIPELINE_FILE" 2>/dev/null)

      # Three-state gate enforcement:
      #   true          = PASS → block tools (no post-gate changes)
      #   "remediation" = FAIL → allow tools (agent fixing issues)
      #   absent        = not yet run → allow tools (normal work)
      #
      # Once the first gate is invoked (ANY_RAN), tools are blocked
      # UNLESS we are in remediation mode (at least one gate failed).
      ANY_RAN=$(echo "$GATE_RESULTS" | jq '[to_entries[]] | length > 0')
      if [ -n "$NEXT_GATE" ] && [ "$ANY_RAN" = "true" ]; then
        HAS_REMEDIATION=$(echo "$GATE_RESULTS" | jq 'to_entries | any(.value == "remediation")')
        if [ "$HAS_REMEDIATION" = "true" ]; then
          : # Remediation mode — tools allowed for fixes
        else
          PHASE_NAME=$(jq -r ".phases[$IDX].name" "$PIPELINE_FILE")
          echo "Gate enforcement: Cannot continue — pending gate agent '${NEXT_GATE}' has not passed for
${PHASE_NAME}. Invoke ${NEXT_GATE} before continuing." >&2
          exit 2
        fi
      fi

      # Block git commit if gates haven't all passed.
      if [ "$TOOL_NAME" = "Bash" ]; then
        COMMAND=$(echo "$PAYLOAD" | jq -r '.tool_input.command // ""')

        if echo "$COMMAND" | grep -qP '(?<![#"\x27])git\s+commit\b'; then
          if [ -n "$NEXT_GATE" ]; then
            PHASE_NAME=$(jq -r ".phases[$IDX].name" "$PIPELINE_FILE")
            echo "Gate enforcement: Cannot commit — gate agent '${NEXT_GATE}' has not passed for ${PHASE_NAME}.
Run all gate agents before committing." >&2
            exit 2
          fi
        fi

        # HARD BLOCK all Bash references to phase-state.json.
        if echo "$COMMAND" | grep -qF 'phase-state'; then
          echo "Gate enforcement: Cannot reference phase-state.json in Bash — use Read tool to inspect, Agent
tool to invoke gates. All state mutations are managed exclusively by the stop hook." >&2
          exit 2
        fi

        # Scan contents of ALL files referenced in the Bash command AND
        # all inline code strings.
        INLINE_CODE=$(echo "$COMMAND" | \
          grep -oP '(?:-c|-e|-Command)\s+["\x27]([^"\x27]*)["\x27]' || true)
        if echo "$INLINE_CODE" | grep -qF 'phase-state'; then
          echo "Gate enforcement: Inline code references phase-state.json — blocked. All state mutations are
managed exclusively by the stop hook." >&2
          exit 2
        fi

        ALL_TOKENS=$(echo "$COMMAND" | tr ' \t;|&' '\n' | \
          grep -E '\.(sh|py|bash|pl|rb|js|ps1|psm1|bat|cmd|php|lua|tcl)$|^\.?/' | \
          sort -u || true)
        EXEC_TARGETS=$(echo "$COMMAND" | \
          grep -oP
'(?:bash|sh|source|python|python3|perl|ruby|node|pwsh|powershell)\s+(?:-\S+\s+)*\K[^\s;|&"'\'']+' || true)
        CAT_TARGETS=$(echo "$COMMAND" | \
          grep -oP '(?:cat|type)\s+\K[^\s;|&]+' || true)

        for token in $ALL_TOKENS $EXEC_TARGETS $CAT_TARGETS; do
          [ -z "$token" ] && continue
          if [ -f "$token" ]; then
            if grep -qF 'phase-state' "$token" 2>/dev/null; then
              echo "Gate enforcement: File '${token}' contains phase-state.json references — execution blocked.
All state mutations are managed exclusively by the stop hook." >&2
              exit 2
            fi
          fi
        done
      fi
    fi
  fi
fi

# ── Filesystem boundary enforcement ──────────────────────────────────────────
PROJECT_ROOT=$(pwd)
PROJECT_PARENT=$(dirname "$PROJECT_ROOT")

is_allowed_external_path() {
  local path="$1"
  path="${path%/}"

  [[ "$path" == "$PROJECT_ROOT"* ]] && return 0

  local claude_dir
  claude_dir=$(eval echo "~/.claude")
  [[ "$path" == "$claude_dir"* ]] && return 0

  [[ "$path" == /usr/bin* ]]       && return 0
  [[ "$path" == /usr/local/bin* ]] && return 0
  [[ "$path" == /usr/lib* ]]       && return 0
  [[ "$path" == /bin* ]]           && return 0
  [[ "$path" == /opt* ]]           && return 0

  [[ "$path" == /c/Windows* ]]     && return 0
  [[ "$path" == /c/Program\ Files* ]] && return 0
  [[ "$path" == /c/Program\ Files\ \(x86\)* ]] && return 0
  [[ "$path" == /c/ProgramData* ]] && return 0
  [[ "$path" == /c/Users/*/AppData* ]] && return 0

  local home_dir="$HOME"
  [[ "$path" == "$home_dir/.nvm"* ]]   && return 0
  [[ "$path" == "$home_dir/.pyenv"* ]] && return 0
  [[ "$path" == "$home_dir/.rbenv"* ]] && return 0
  [[ "$path" == "$home_dir/.cargo"* ]] && return 0
  [[ "$path" == "$home_dir/.go"* ]]    && return 0

  [[ "$path" == /tmp* ]]       && return 0
  [[ "$path" == /tmp/* ]]      && return 0
  [[ "$path" == /var/tmp* ]]   && return 0
  [[ "$path" == /dev/shm* ]]   && return 0
  [[ "$path" == /temp* ]]      && return 0

  [[ "$path" == /c/Users/*/AppData/Local/Temp* ]] && return 0
  [[ "$path" == /c/Windows/Temp* ]]               && return 0

  if [ -n "${TMPDIR:-}" ]; then
    [[ "$path" == "${TMPDIR}"* ]] && return 0
  fi

  [[ "$path" == /var/log* ]]                   && return 0
  [[ "$path" == /var/log/* ]]                  && return 0
  [[ "$path" == "$home_dir/.local/share/logs"* ]] && return 0

  [[ "$path" == /c/Windows/System32/winevt* ]] && return 0

  local extra_paths_file="$PROJECT_ROOT/.claude/allowed-paths.json"
  if [ -f "$extra_paths_file" ]; then
    while IFS= read -r allowed; do
      [[ "$path" == "$allowed"* ]] && return 0
    done < <(jq -r '.extra_allowed_paths[]' "$extra_paths_file" 2>/dev/null)
  fi

  return 1
}

is_sibling_or_parent_path() {
  local path="$1"
  path="${path%/}"

  [[ "$path" == "$PROJECT_PARENT" ]]   && return 0
  [[ "$path" == "$PROJECT_PARENT"/ ]]  && return 0

  if [[ "$path" == "$PROJECT_PARENT/"* ]]; then
    [[ "$path" == "$PROJECT_ROOT"* ]] || return 0
  fi

  return 1
}

check_path_boundary() {
  local path="$1"

  [[ "$path" != /* ]] && return 0

  path="${path%/}"

  is_allowed_external_path "$path" && return 0

  [[ "$path" == "$PROJECT_ROOT"* ]] && return 0

  echo "Filesystem boundary: access to '${path}' is not permitted. Allowed: project directory (${PROJECT_ROOT}),
 ~/.claude/, system bin/lib paths. To explore system directories like /var/log or /tmp, request explicit user
approval first." >&2
  exit 2
}

if [[ "$TOOL_NAME" == "Read" ]]; then
  FILE_PATH=$(echo "$PAYLOAD" | jq -r '.tool_input.file_path // .tool_input.path // ""')
  check_path_boundary "$FILE_PATH"
fi

if [[ "$TOOL_NAME" == "Glob" || "$TOOL_NAME" == "Grep" ]]; then
  FILE_PATH=$(echo "$PAYLOAD" | jq -r '.tool_input.path // ""')
  check_path_boundary "$FILE_PATH"
fi

if [[ "$TOOL_NAME" == "Write" || "$TOOL_NAME" == "Edit" || "$TOOL_NAME" == "MultiEdit" ]]; then
  FILE_PATH=$(echo "$PAYLOAD" | jq -r '.tool_input.file_path // ""')
  check_path_boundary "$FILE_PATH"
fi

if [[ "$TOOL_NAME" == "Bash" ]]; then
  COMMAND=$(echo "$PAYLOAD" | jq -r '.tool_input.command // ""')

  ALL_PATHS=$(echo "$COMMAND" | \
    grep -oP '(?:^|(?<=\s)|(?<=[;|&>]))(/[a-zA-Z0-9_.@/-]+)' | \
    sort -u || true)

  while IFS= read -r ABS_PATH; do
    [ -z "$ABS_PATH" ] && continue
    check_path_boundary "$ABS_PATH"
  done <<< "$ALL_PATHS"
fi

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
      echo "{\"action\": \"ask\", \"reason\": \"⚠️  Network operation detected: \`${COMMAND:0:80}\` — approval
needed.\"}"
      exit 0
    fi
  done
fi

# ── All other tool uses: allow silently ───────────────────────────────────────
echo '{"action": "allow"}'
exit 0