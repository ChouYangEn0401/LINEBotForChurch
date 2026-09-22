#!/usr/bin/env bash
# 給其他程式呼叫（Telegram 機器人、排程）：不問問題、跑完就結束。
# 例：cli.sh send --scheduled（到時間、還沒發才發）、cli.sh send、cli.sh preview、cli.sh check
source "$(dirname "$0")/_common.sh"
require_venv
exec "$VENV_PY" -m church_bot "$@"
