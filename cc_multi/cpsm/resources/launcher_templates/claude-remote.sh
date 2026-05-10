#!/bin/bash
# -*- coding: utf-8 -*-
# CPSM claude-remote launcher template.
# Placeholders rendered by TemplateService.render() before execution.
# All placeholder values are shlex.quote'd by the renderer (safe_render=True).
#
# Placeholders (required unless noted optional):
#   connection_id   — unique connection slug (used in remote script path)
#   ssh_options     — (optional) extra SSH flags from settings.default_ssh_options
#   identity_file   — (optional) path to identity file
#   user            — SSH login username
#   host            — remote hostname or IP
#   sudo_user       — (optional) if non-empty, run via "su - <sudo_user> -c ..."
#   project_folder  — remote directory to cd into
#   claude_options  — options to pass to claude
set -o pipefail
# Enable job control so the ssh child gets its own process group and terminal
# foreground control. Without this, tmux's pane_current_command reports the
# outer bash even while ssh is the active connection — which makes the UI
# status-deriver flag every connected pane as "dropped" (amber).
set -m

_CONN_ID={{connection_id}}
_REMOTE_SCRIPT="/tmp/cpsm-remote-${_CONN_ID}.sh"
_USER={{user}}
_HOST={{host}}
_SUDO_USER={{sudo_user|}}
_PROJECT_FOLDER={{project_folder}}
_CLAUDE_OPTIONS={{claude_options}}
_SSH_OPTIONS={{ssh_options|}}
_IDENTITY_FILE={{identity_file|}}

# Build identity file argument (only when non-empty)
_ID_ARG=""
if [ -n "${_IDENTITY_FILE}" ]; then
    _ID_ARG="-i ${_IDENTITY_FILE}"
fi

# ---------------------------------------------------------------------------
# Write the remote helper script locally using a QUOTED heredoc.
# A quoted heredoc delimiter ('REMOTE_SCRIPT_EOF') tells bash to perform NO
# parameter expansion when writing the body — values that look like
# $(cmd) or ${VAR} are written verbatim.  The remote script receives its
# parameters via positional arguments ($1, $2) passed at invocation time.
# ---------------------------------------------------------------------------
_write_remote_script() {
    cat > "${_REMOTE_SCRIPT}" << 'REMOTE_SCRIPT_EOF'
#!/bin/bash -il
# CPSM remote helper — runs on target host. Args: $1=project_folder, $2=claude_options
[ -f ~/.bashrc ] && source ~/.bashrc 2>/dev/null || true
[ -f ~/.profile ] && source ~/.profile 2>/dev/null || true

export TERM=xterm-256color
export LANG=C.UTF-8
clear

cd "$1" 2>/dev/null || {
    echo "ERROR: Cannot cd to $1"
    echo "Current directory: $(pwd)"
    echo "Continuing anyway..."
}

echo "Starting Claude..."
claude $2 || {
    _claude_exit=$?
    echo "Claude exited with status: ${_claude_exit}"
    echo "Press Enter for shell..."
    read -r _ignored
    exec bash
}
REMOTE_SCRIPT_EOF
    # 0755 instead of 0700: the remote script is world-readable so an
    # optional `su - <sudo_user>` invocation can still bash -il it. The
    # script itself contains no secrets.
    chmod 0755 "${_REMOTE_SCRIPT}"
}

# ---------------------------------------------------------------------------
# Connect: SCP script to remote, then SSH and run it
# ---------------------------------------------------------------------------
_connect() {
    _write_remote_script

    # Remove any stale remote helper from a previous user/run before SCP'ing.
    # If a different user owned the existing file with restrictive perms,
    # SCP would either fail or leave it unreadable to the current SSH user.
    # shellcheck disable=SC2086
    ssh ${_ID_ARG} ${_SSH_OPTIONS} "${_USER}@${_HOST}" \
        "rm -f ${_REMOTE_SCRIPT} 2>/dev/null; true" >/dev/null 2>&1 || true

    # Upload the script via SCP; fall back to SSH pipe if SCP fails. Errors
    # from both paths are no longer suppressed so the user can see what
    # actually went wrong (e.g. wrong password, SSH key not deployed,
    # connection refused).
    # shellcheck disable=SC2086
    if ! scp ${_ID_ARG} ${_SSH_OPTIONS} \
            "${_REMOTE_SCRIPT}" \
            "${_USER}@${_HOST}:${_REMOTE_SCRIPT}"; then
        echo "SCP failed, falling back to SSH pipe..." >&2
        # shellcheck disable=SC2086
        if ! ssh ${_ID_ARG} ${_SSH_OPTIONS} "${_USER}@${_HOST}" \
                "cat > ${_REMOTE_SCRIPT} && chmod 0755 ${_REMOTE_SCRIPT}" \
                < "${_REMOTE_SCRIPT}"; then
            echo "ERROR: could not upload remote helper script to ${_USER}@${_HOST}." >&2
            echo "Check SSH credentials / network and retry." >&2
            return 1
        fi
    fi
    # Ensure the uploaded file is readable, regardless of SCP's mode-preservation
    # behaviour.
    # shellcheck disable=SC2086
    ssh ${_ID_ARG} ${_SSH_OPTIONS} "${_USER}@${_HOST}" \
        "chmod 0755 ${_REMOTE_SCRIPT}" >/dev/null 2>&1 || true

    # Execute remotely.
    # Non-sudo path: SSH receives argv [bash, -il, <script>, <arg1>, <arg2>] as
    # separate words — no string-parsing layer, no injection.
    # Sudo path: printf '%q' re-quotes each value for the additional shell layers
    # introduced by the remote shell parsing the SSH command string, then su -c
    # parsing the inner string.
    if [ -n "${_SUDO_USER}" ]; then
        # shellcheck disable=SC2086
        ssh -tt ${_ID_ARG} ${_SSH_OPTIONS} "${_USER}@${_HOST}" \
            "chmod 0755 ${_REMOTE_SCRIPT} && su - $(printf '%q' "${_SUDO_USER}") -c 'bash -il $(printf '%q' "${_REMOTE_SCRIPT}") $(printf '%q' "${_PROJECT_FOLDER}") $(printf '%q' "${_CLAUDE_OPTIONS}")'"
    else
        # shellcheck disable=SC2086
        ssh -tt ${_ID_ARG} ${_SSH_OPTIONS} "${_USER}@${_HOST}" \
            bash -il "${_REMOTE_SCRIPT}" "${_PROJECT_FOLDER}" "${_CLAUDE_OPTIONS}"
    fi
}

# ---------------------------------------------------------------------------
# Cleanup: remove the remote script on exit
# ---------------------------------------------------------------------------
_cleanup_remote() {
    # shellcheck disable=SC2086
    ssh ${_ID_ARG} ${_SSH_OPTIONS} "${_USER}@${_HOST}" \
        "rm -f ${_REMOTE_SCRIPT}" 2>/dev/null || true
    rm -f "${_REMOTE_SCRIPT}" 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# [r/s/q] reconnect loop — mirrors claude-multi-manager.sh
# ---------------------------------------------------------------------------
while true; do
    _connect
    _result=$?

    echo ""
    if [ "${_result}" -eq 0 ]; then
        echo "Session ended normally"
    else
        echo "Connection failed (exit: ${_result})"
    fi

    echo -n "[r]econnect, [s]hell, [q]uit: "
    read -n 1 -r _response
    echo ""

    case "${_response}" in
        s|S) bash ;;
        q|Q)
            _cleanup_remote
            exit 0
            ;;
        *) echo "Reconnecting..." ;;
    esac
done
