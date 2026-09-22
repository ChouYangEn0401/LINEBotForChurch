#!/usr/bin/env bash
# 給 Telegram 機器人（負責排程）等其他程式呼叫：不問問題、跑完就結束。
# 例：cli.sh send --retries 3 --retry-wait 300、cli.sh send、cli.sh preview、cli.sh check
source "$(dirname "$0")/_common.sh"
require_venv
exec "$VENV_PY" -m church_bot "$@"
