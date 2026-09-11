#!/usr/bin/env bash
# Mac：取消開機自動執行（並停止背景中的程式）。
set -euo pipefail
LABEL="tw.church-bot"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
rm -f "$PLIST"
echo "✅ 已取消開機自動執行，背景程式也停止了。"
