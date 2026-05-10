#!/bin/bash
# -*- coding: utf-8 -*-
# CPSM ssh-shell launcher template.
# Placeholders rendered by TemplateService.render() before execution.
# All placeholder values are shlex.quote'd by the renderer (safe_render=True).
#
# Placeholders:
#   ssh_options     — (optional) extra SSH flags from settings.default_ssh_options
#   identity_file   — (optional) path to identity file
#   user            — SSH login username
#   host            — remote hostname or IP
#   project_folder  — (optional) remote directory to cd into
set -o pipefail

_SSH_OPTIONS={{ssh_options|}}
_IDENTITY_FILE={{identity_file|}}
_USER={{user}}
_HOST={{host}}
_PROJECT_FOLDER={{project_folder|}}

# Build identity file argument (only when non-empty)
_ID_ARG=""
if [ -n "${_IDENTITY_FILE}" ]; then
    _ID_ARG="-i ${_IDENTITY_FILE}"
fi

# Pass project_folder as a positional argument via `bash -ilc '...' -- "$1"` so
# the value is never interpolated into a double-quoted string that a remote shell
# would re-parse — no injection possible regardless of what project_folder contains.
if [ -n "${_PROJECT_FOLDER}" ]; then
    # shellcheck disable=SC2086
    exec ssh -tt ${_ID_ARG} ${_SSH_OPTIONS} "${_USER}@${_HOST}" \
        bash -ilc 'cd "$1" && exec bash -il' -- "${_PROJECT_FOLDER}"
else
    # shellcheck disable=SC2086
    exec ssh -tt ${_ID_ARG} ${_SSH_OPTIONS} "${_USER}@${_HOST}" -- exec bash -il
fi
