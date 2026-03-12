#!/bin/bash
# claude-multi-manager-final.sh - Correct version with SCP and su -

set -o pipefail

CONFIG_FILE="${CLAUDE_CONFIG:-$HOME/.claude-projects.yaml}"
DEBUG="true"

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# Parse config
parse_config() {
    if [ ! -f "$CONFIG_FILE" ]; then
        log_error "Configuration file not found: $CONFIG_FILE"
        exit 1
    fi
    
    export PROJECTS_JSON=$(python3 -c "
import yaml, json
with open('$CONFIG_FILE', 'r') as f:
    content = f.read()
    if '...' in content:
        content = content.split('...')[0]
    config = yaml.safe_load(content)
    print(json.dumps(config.get('projects', [])))" 2>/dev/null)
}

# Get groups and projects
get_unique_groups() {
    echo "$PROJECTS_JSON" | jq -r '.[].group' | sort -u
}

get_projects_by_group() {
    local group="$1"
    echo "$PROJECTS_JSON" | jq -c ".[] | select(.group == \"$group\")"
}

get_project_by_name() {
    local name="$1"
    echo "$PROJECTS_JSON" | jq -c ".[] | select(.name == \"$name\")" | head -n1
}

get_all_project_names() {
    echo "$PROJECTS_JSON" | jq -r '.[].name'
}

# Create launcher WITH SCP and su -
create_launcher() {
    local name="$1"
    local host="$2"
    local ssh_user="$3"
    local sudo_user="$4"
    local folder="$5"
    local claude_opts="$6"
    
    # Use consistent naming
    local safe_name=$(echo "$name" | tr '[:upper:]' '[:lower:]' | tr -cd '[:alnum:]')
    local launcher_script="/tmp/launcher-${safe_name}.sh"
    local remote_script="/tmp/remote-${safe_name}.sh"
    
    # Create remote script locally (no escaping issues!)
    cat > "$remote_script" << REMOTE_SCRIPT
#!/bin/bash -il
# This runs on remote host as $sudo_user

# Load full environment (needed for aliases)
[ -f ~/.bashrc ] && source ~/.bashrc
[ -f ~/.profile ] && source ~/.profile

export TERM=xterm-256color
export LANG=C.UTF-8
clear

echo "================================"
echo "Project: $name"
echo "Directory: $folder"
echo "================================"

# Change to project directory
cd "$folder" 2>/dev/null || {
    echo "ERROR: Cannot cd to $folder"
    echo "Current: \$(pwd)"
    echo "Continuing anyway..."
}

# Run Claude (as alias)
echo "Starting Claude..."
claude $claude_opts || {
    echo "Claude exited with status: \$?"
    echo "Press Enter for shell..."
    read
    bash
}
REMOTE_SCRIPT
    
    # Create launcher script
    cat > "$launcher_script" << LAUNCHER
#!/bin/bash
echo -e "${GREEN}================================${NC}"
echo -e "${GREEN}  $name${NC}"
echo -e "${GREEN}================================${NC}"
echo "Host: $ssh_user@$host"
echo ""

connect() {
    # First, ensure remote script exists locally
    if [ ! -f "$remote_script" ]; then
        echo "ERROR: Remote script not found at $remote_script"
        echo "Creating it now..."
        # Recreate the script here if needed
        cat > "$remote_script" << 'REMAKE'
#!/bin/bash -il
[ -f ~/.bashrc ] && source ~/.bashrc
[ -f ~/.profile ] && source ~/.profile
cd $folder 2>/dev/null
claude $claude_opts || bash
REMAKE
        chmod +x "$remote_script"
    fi
    
    # Upload script via SCP
    echo "Uploading script..."
    
    # Try SCP with absolute path
    if scp -q "$remote_script" "$ssh_user@$host:/tmp/remote-${safe_name}.sh" 2>/dev/null; then
        echo "Script uploaded successfully"
    else
        echo "SCP failed, trying alternative method..."
        # Alternative: copy content via SSH
        ssh "$ssh_user@$host" "cat > /tmp/remote-${safe_name}.sh" < "$remote_script"
    fi
    
    # Connect and run with su -
    echo "Connecting..."
    ssh -tt "$ssh_user@$host" "
        # Make sure script exists and is executable
        if [ ! -f /tmp/remote-${safe_name}.sh ]; then
            echo 'ERROR: Remote script not found after upload!'
            exit 1
        fi
        
        chmod +x /tmp/remote-${safe_name}.sh
        echo 'Switching to user $sudo_user...'
        
        # Use su - for full environment
        su - $sudo_user -c 'bash -il /tmp/remote-${safe_name}.sh'
        
        # Clean up
        rm -f /tmp/remote-${safe_name}.sh
    "
}

# Main loop
while true; do
    connect
    result=\$?
    
    echo ""
    if [ \$result -eq 0 ]; then
        echo "Session ended normally"
    else
        echo "Connection failed (exit: \$result)"
    fi
    
    echo "[r]econnect, [s]hell, [q]uit: "
    read -n 1 -r response
    echo ""
    
    case "\$response" in
        s|S) bash ;;
        q|Q) 
            rm -f "$remote_script"
            exit 0
            ;;
        *) 
            echo "Reconnecting..."
            continue
            ;;
    esac
done
LAUNCHER
    
    chmod +x "$launcher_script"
    chmod +x "$remote_script"
    
    # Verify both scripts exist
    if [ ! -f "$launcher_script" ] || [ ! -f "$remote_script" ]; then
        log_error "Failed to create scripts for $name"
        return 1
    fi
    
    echo "$launcher_script"
}

# Launch single project by name (runs in current terminal)
launch_single() {
    local project_name="$1"
    local project=$(get_project_by_name "$project_name")

    if [ -z "$project" ] || [ "$project" = "null" ]; then
        log_error "Project not found: $project_name"
        return 1
    fi

    local name=$(echo "$project" | jq -r '.name')
    local host=$(echo "$project" | jq -r '.host')
    local ssh_user=$(echo "$project" | jq -r '.ssh_user')
    local sudo_user=$(echo "$project" | jq -r '.sudo_user')
    local folder=$(echo "$project" | jq -r '.project_folder')
    local claude_opts=$(echo "$project" | jq -r '.claude_options // "--resume"')

    [ "$claude_opts" = "null" ] && claude_opts="--resume"

    log_info "Launching single project: $name"
    local launcher=$(create_launcher "$name" "$host" "$ssh_user" "$sudo_user" "$folder" "$claude_opts")

    if [ -z "$launcher" ] || [ ! -f "$launcher" ]; then
        log_error "Failed to create launcher for $name"
        return 1
    fi

    # Execute launcher directly in current terminal
    exec bash "$launcher"
}

# Launch group
launch_group() {
    local group="$1"
    local session_name="claude-$(echo "$group" | tr '[:upper:]' '[:lower:]')"
    
    log_info "Creating session: $session_name"
    
    # Kill existing session
    tmux kill-session -t "$session_name" 2>/dev/null
    sleep 0.5
    
    # Get projects
    local projects=()
    while IFS= read -r proj; do
        [ -n "$proj" ] && projects+=("$proj")
    done < <(get_projects_by_group "$group")
    
    if [ ${#projects[@]} -eq 0 ]; then
        log_error "No projects in group $group"
        return 1
    fi
    
    # Create new session
    tmux new-session -d -s "$session_name" -n "$group"
    
    # Set options
    tmux set -t "$session_name" -g pane-border-status top
    tmux set -t "$session_name" -g pane-border-format "#{pane_index}: #{pane_title}"
    tmux set -t "$session_name" -g default-terminal "screen-256color"
    
    # Launch projects
    local max=4
    [ ${#projects[@]} -lt $max ] && max=${#projects[@]}
    
    for ((i=0; i<max; i++)); do
        local project="${projects[$i]}"
        local name=$(echo "$project" | jq -r '.name')
        local host=$(echo "$project" | jq -r '.host')
        local ssh_user=$(echo "$project" | jq -r '.ssh_user')
        local sudo_user=$(echo "$project" | jq -r '.sudo_user')
        local folder=$(echo "$project" | jq -r '.project_folder')
        local claude_opts=$(echo "$project" | jq -r '.claude_options // "--resume"')
        
        [ "$claude_opts" = "null" ] && claude_opts="--resume"
        
        log_info "  Creating launcher for $name"
        local launcher=$(create_launcher "$name" "$host" "$ssh_user" "$sudo_user" "$folder" "$claude_opts")
        
        if [ -z "$launcher" ] || [ ! -f "$launcher" ]; then
            log_error "  Failed to create launcher for $name"
            continue
        fi
        
        if [ $i -eq 0 ]; then
            tmux respawn-pane -t "$session_name:0.0" -k "bash '$launcher'"
            tmux select-pane -t "$session_name:0.0" -T "$name"
        else
            tmux split-window -t "$session_name:0" "bash '$launcher'"
            tmux select-pane -t "$session_name:0.$i" -T "$name"
        fi
        
        sleep 0.5
    done
    
    tmux select-layout -t "$session_name:0" tiled
    
    # Verify
    if tmux has-session -t "$session_name" 2>/dev/null; then
        log_info "✓ Session $session_name created"
        return 0
    else
        log_error "✗ Failed to create $session_name"
        return 1
    fi
}

# Main
main() {
    log_info "Claude Multi-Manager - Final Version"
    
    parse_config
    
    readarray -t groups < <(get_unique_groups)
    log_info "Groups: ${groups[*]}"
    
    case "$1" in
        all)
            launched_sessions=()
            for group in "${groups[@]}"; do
                launch_group "$group"
                session_name="claude-$(echo "$group" | tr '[:upper:]' '[:lower:]')"
                launched_sessions+=("$session_name")
                sleep 1
            done

            echo ""
            echo "========================================="
            echo "SESSIONS CREATED:"
            tmux ls 2>/dev/null | grep "^claude-"
            echo ""
            echo "To attach:"
            for i in "${!launched_sessions[@]}"; do
                echo "  tmux attach -t ${launched_sessions[$i]}"
            done
            echo "========================================="
            ;;

        "")
            echo "Usage: $0 <project|group|all|--help>"
            echo "Available groups: ${groups[*]}"
            echo "Available projects: $(get_all_project_names | tr '\n' ' ')"
            echo "Run '$0 --help' for more information."
            ;;

        --help|-h|help)
            echo ""
            echo "Claude Multi-Manager"
            echo "===================="
            echo ""
            echo "USAGE:"
            echo "  $0 <project>   Launch a single project in current terminal"
            echo "  $0 <group>     Launch a group in a new tmux session"
            echo "  $0 all         Launch all groups"
            echo "  $0 --help      Show this help message"
            echo ""
            echo "AVAILABLE GROUPS AND PROJECTS:"
            for group in "${groups[@]}"; do
                session_name="claude-$(echo "$group" | tr '[:upper:]' '[:lower:]')"
                echo ""
                echo "  $group (session: $session_name)"
                while IFS= read -r proj; do
                    [ -z "$proj" ] && continue
                    name=$(echo "$proj" | jq -r '.name')
                    host=$(echo "$proj" | jq -r '.host')
                    folder=$(echo "$proj" | jq -r '.project_folder')
                    echo "    - $name"
                    echo "        Host: $host"
                    echo "        Path: $folder"
                done < <(get_projects_by_group "$group")
            done
            echo ""
            echo "EXAMPLES:"
            echo "  Launch a single project in current terminal (for reconnecting in existing pane):"
            first_project=$(get_all_project_names | head -n1)
            echo "    $0 $first_project"
            echo ""
            echo "  Launch a group in new tmux session:"
            echo "    $0 ${groups[0]}"
            echo ""
            echo "  Launch all groups:"
            echo "    $0 all"
            echo ""
            echo "  Attach to a session after launching:"
            first_session="claude-$(echo "${groups[0]}" | tr '[:upper:]' '[:lower:]')"
            echo "    tmux attach -t $first_session"
            echo ""
            echo "CONFIGURATION:"
            echo "  Config file: $CONFIG_FILE"
            echo ""
            ;;

        *)
            # First, check if argument is a project name (exact match)
            readarray -t all_projects < <(get_all_project_names)
            matched_project=""
            for p in "${all_projects[@]}"; do
                if [[ "$p" == "$1" ]]; then
                    matched_project="$p"
                    break
                fi
            done

            if [[ -n "$matched_project" ]]; then
                launch_single "$matched_project"
                exit $?
            fi

            # Check if argument is a valid group (case-insensitive)
            matched_group=""
            for g in "${groups[@]}"; do
                if [[ "${g,,}" == "${1,,}" ]]; then
                    matched_group="$g"
                    break
                fi
            done

            if [[ -n "$matched_group" ]]; then
                launch_group "$matched_group"
                session_name="claude-$(echo "$matched_group" | tr '[:upper:]' '[:lower:]')"
                echo ""
                echo "========================================="
                echo "SESSION CREATED:"
                tmux ls 2>/dev/null | grep "^claude-"
                echo ""
                echo "To attach:"
                echo "  tmux attach -t $session_name"
                echo "========================================="
            else
                echo "Unknown group or project: $1"
                echo "Available groups: ${groups[*]}"
                echo "Available projects: ${all_projects[*]}"
            fi
            ;;
    esac
}

# Make sure we have a /tmp directory
[ -d /tmp ] || mkdir -p /tmp

# Clean up old scripts
rm -f /tmp/launcher-*.sh /tmp/remote-*.sh 2>/dev/null

# Run
main "$@"