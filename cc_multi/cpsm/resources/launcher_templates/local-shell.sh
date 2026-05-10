#!/bin/bash
# -*- coding: utf-8 -*-
# CPSM local-shell launcher template.
# Placeholders rendered by TemplateService.render() before execution.
# All placeholder values are shlex.quote'd by the renderer (safe_render=True).
#
# Placeholders:
#   project_folder  — local directory to cd into
set -o pipefail

_PROJECT_FOLDER={{project_folder}}

cd "${_PROJECT_FOLDER}"
exec ${SHELL:-/bin/bash} -il
