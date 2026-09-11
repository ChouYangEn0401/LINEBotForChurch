#!/usr/bin/env bash
# 啟動管理網頁 + 自動排程。這個視窗要一直開著，每週才會自動提醒。
source "$(dirname "$0")/_common.sh"
require_venv
exec "$VENV_PY" -m church_bot web "$@"
