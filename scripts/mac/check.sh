#!/usr/bin/env bash
# 健康檢查：設定、服事表、LINE 是否都正常，並預覽這次會發的訊息（不會真的送出）。
source "$(dirname "$0")/_common.sh"
require_venv
"$VENV_PY" -m church_bot check "$@"
