#!/bin/bash
# -*- coding: utf-8 -*-
# CPSM claude-local launcher template.
# Placeholders rendered by TemplateService.render() before execution.
# All placeholder values are shlex.quote'd by the renderer (safe_render=True).
#
# Placeholders:
#   project_folder  — local directory to cd into
#   claude_options  — options to pass to claude
#   sudo_user       — (optional) if non-empty, run claude as this user via sudo -u ... -i
set -o pipefail

_PROJECT_FOLDER={{project_folder}}
_CLAUDE_OPTIONS={{claude_options}}
_SUDO_USER={{sudo_user|}}

_connect() {
    cd "${_PROJECT_FOLDER}" || {
        echo "ERROR: Cannot cd to ${_PROJECT_FOLDER}"
        return 1
    }

    # Run claude as a child process (NOT exec) so control returns to the [r/s/q] loop on exit.
    # The single-quoted bash -ic body uses $1/$2 as positional argv so the values
    # are never re-parsed by a second shell layer — no injection possible.
    if [ -n "${_SUDO_USER}" ]; then
        sudo -u "${_SUDO_USER}" -i bash -ic 'cd "$1" && claude $2' -- \
            "${_PROJECT_FOLDER}" "${_CLAUDE_OPTIONS}"
    else
        claude ${_CLAUDE_OPTIONS}
    fi
}

# [r/s/q] reconnect loop — mirrors claude-multi-manager.sh
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
        q|Q) exit 0 ;;
        *) echo "Reconnecting..." ;;
    esac
done
