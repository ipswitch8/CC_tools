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

# Exit code 2 sends the message directly to Claude as a tool error —
# it cannot proceed and sees exactly why.
#
# Why ANY_RAN gate:
# The block only activates once at least one gate has run. This prevents
# it from blocking the phase work itself (when current_gate_results is
# empty, Claude should be free to write). The sequence becomes:
#
#   current_gate_results = {}       → writing allowed  (phase work in progress)
#   karen = true, validator = ?     → writing BLOCKED  (gates started, not finished)
#   karen = true, validator = true  → writing allowed  (all gates passed, /g pending)
#   awaiting_commit = true          → writing BLOCKED  (waiting for /g)
#   phase advances, gates reset     → writing allowed  (next phase begins)

# ── Filesystem boundary enforcement ──────────────────────────────────────────
# Resolve the current project root (where claude was launched from)
PROJECT_ROOT=$(pwd)
PROJECT_PARENT=$(dirname "$PROJECT_ROOT")

# Paths allowed outside the project root
is_allowed_external_path() {
  local path="$1"
  # Normalize: strip trailing slash
  path="${path%/}"

  # Allow anything inside the project
  [[ "$path" == "$PROJECT_ROOT"* ]] && return 0

  # Allow ~/.claude and subdirectories
  local claude_dir
  claude_dir=$(eval echo "~/.claude")
  [[ "$path" == "$claude_dir"* ]] && return 0

  # Allow system binary and library paths (Linux/Mac)
  [[ "$path" == /usr/bin* ]]       && return 0
  [[ "$path" == /usr/local/bin* ]] && return 0
  [[ "$path" == /usr/lib* ]]       && return 0
  [[ "$path" == /bin* ]]           && return 0
  [[ "$path" == /opt* ]]           && return 0

  # Allow Windows system paths (Git Bash)
  [[ "$path" == /c/Windows* ]]     && return 0
  [[ "$path" == /c/Program\ Files* ]] && return 0
  [[ "$path" == /c/Program\ Files\ \(x86\)* ]] && return 0
  [[ "$path" == /c/ProgramData* ]] && return 0
  [[ "$path" == /c/Users/*/AppData* ]] && return 0

  # Allow nvm, pyenv, rbenv, etc.
  local home_dir="$HOME"
  [[ "$path" == "$home_dir/.nvm"* ]]   && return 0
  [[ "$path" == "$home_dir/.pyenv"* ]] && return 0
  [[ "$path" == "$home_dir/.rbenv"* ]] && return 0
  [[ "$path" == "$home_dir/.cargo"* ]] && return 0
  [[ "$path" == "$home_dir/.go"* ]]    && return 0

  # Allow read/write to temp directories (prefix match)
  [[ "$path" == /tmp* ]]       && return 0            
  [[ "$path" == /tmp/* ]]      && return 0                                                                           
  [[ "$path" == /var/tmp* ]]   && return 0
  [[ "$path" == /dev/shm* ]]   && return 0                                                                           
  [[ "$path" == /temp* ]]      && return 0
                                                                                                                     
  # Windows temp paths (Git Bash)
  [[ "$path" == /c/Users/*/AppData/Local/Temp* ]] && return 0                                                        
  [[ "$path" == /c/Windows/Temp* ]]               && return 0
                                                                                                                     
  # TMPDIR override (if set)                                                                                         
  if [ -n "${TMPDIR:-}" ]; then                                                                                      
    [[ "$path" == "${TMPDIR}"* ]] && return 0                                                                        
  fi                                                                                                                 

  # Allow read access to common log locations (prefix match)                                                         
  [[ "$path" == /var/log* ]]                   && return 0                                                           
  [[ "$path" == /var/log/* ]]                  && return 0
  [[ "$path" == "$home_dir/.local/share/logs"* ]] && return 0                                                        
                                                             
  # Windows event logs (Git Bash)                                                                                    
  [[ "$path" == /c/Windows/System32/winevt* ]] && return 0

  # Project-specific extra allowances
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

  # Block exact parent directory or anything above it
  [[ "$path" == "$PROJECT_PARENT" ]]   && return 0
  [[ "$path" == "$PROJECT_PARENT"/ ]]  && return 0

  # Block sibling projects: same parent, different child
  if [[ "$path" == "$PROJECT_PARENT/"* ]]; then
    # It's under the parent — is it our project or something else?
    [[ "$path" == "$PROJECT_ROOT"* ]] || return 0
  fi

  return 1
}

check_path_boundary() {
  local path="$1"

  # Relative paths always fine — resolve inside project
  [[ "$path" != /* ]] && return 0

  # Normalize
  path="${path%/}"

  # ── WHITELIST: explicitly allowed external paths ──────────────────────────
  is_allowed_external_path "$path" && return 0

  # ── PROJECT: anything inside the current project ──────────────────────────
  [[ "$path" == "$PROJECT_ROOT"* ]] && return 0

  # ── EVERYTHING ELSE: block ────────────────────────────────────────────────
  echo "Filesystem boundary: access to '${path}' is not permitted. Allowed: project directory (${PROJECT_ROOT}), ~/.claude/, system bin/lib paths. To explore system directories like /var/log or /tmp, request explicit user approval first." >&2
  exit 2
}

# Check Read tool paths (uses file_path, not path)                                                                                                                                          
if [[ "$TOOL_NAME" == "Read" ]]; then                                                                                                                                                       
  FILE_PATH=$(echo "$PAYLOAD" | jq -r '.tool_input.file_path // .tool_input.path // ""')                                                                                                    
  check_path_boundary "$FILE_PATH"                                                                                                                                                          
fi                                                                                                                                                                                          
																																														  
# Check Glob, Grep tool paths
if [[ "$TOOL_NAME" == "Glob" || "$TOOL_NAME" == "Grep" ]]; then
  FILE_PATH=$(echo "$PAYLOAD" | jq -r '.tool_input.path // ""')
  check_path_boundary "$FILE_PATH"
fi

# Check Write and Edit tools (use file_path)
if [[ "$TOOL_NAME" == "Write" || "$TOOL_NAME" == "Edit" || "$TOOL_NAME" == "MultiEdit" ]]; then
  FILE_PATH=$(echo "$PAYLOAD" | jq -r '.tool_input.file_path // ""')
  check_path_boundary "$FILE_PATH"
fi

# Check Bash commands for ALL absolute paths, not just read commands
if [[ "$TOOL_NAME" == "Bash" ]]; then
  COMMAND=$(echo "$PAYLOAD" | jq -r '.tool_input.command // ""')

  # Extract absolute paths only (must start with / preceded by whitespace,
  # start of string, or shell operator). Avoids false positives from                                                
  # relative paths like .claude/hooks/stage-files.sh where /hooks/...                                               
  # would be incorrectly extracted as an absolute path.                                                             
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
      echo "{\"action\": \"ask\", \"reason\": \"⚠️  Network operation detected: \`${COMMAND:0:80}\` — approval needed.\"}"
      exit 0
    fi
  done
fi

# ── All other tool uses: allow silently ───────────────────────────────────────
echo '{"action": "allow"}'
exit 0
