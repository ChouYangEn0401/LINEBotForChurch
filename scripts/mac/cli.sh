#!/usr/bin/env bash
# 給 Telegram 機器人（負責排程）等其他程式呼叫：不問問題、跑完就結束。
# 例：cli.sh send --retries 3 --retry-wait 300、cli.sh send、cli.sh preview、cli.sh check
# cli.sh quota：查本月 LINE 用量；排在 send 之後約 5 分鐘呼叫，管理網頁看到的就是發送後的用量。
source "$(dirname "$0")/_common.sh"
require_venv
exec "$VENV_PY" -m church_bot "$@"
