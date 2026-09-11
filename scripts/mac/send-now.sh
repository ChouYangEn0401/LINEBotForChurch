#!/usr/bin/env bash
# 立刻發送一次（已經送過的不會重送；要強制重送請加 --force）。
source "$(dirname "$0")/_common.sh"
require_venv
"$VENV_PY" -m church_bot send "$@"
