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
