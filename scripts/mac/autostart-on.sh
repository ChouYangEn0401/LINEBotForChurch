#!/usr/bin/env bash
# Mac：開機（登入）後自動在背景執行，不用每次手動打開。
# 取消請執行 autostart-off.sh。
source "$(dirname "$0")/_common.sh"
require_venv

if [ "$(uname)" != "Darwin" ]; then
  echo "這個檔案只給 Mac 用。Windows 請用 scripts/windows/autostart-on.bat"
  exit 1
fi

case "$ROOT" in
  "$HOME/Documents"*|"$HOME/Desktop"*|"$HOME/Downloads"*)
    echo "⚠️ 程式放在「文件 / 桌面 / 下載」資料夾裡，macOS 可能不允許背景程式讀取這些地方。"
    echo "   如果開機後沒有自動提醒，請把整個資料夾搬到家目錄（例如 $HOME/church-bot），再執行一次這個檔案。"
    ;;
esac

LABEL="tw.church-bot"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
mkdir -p "$HOME/Library/LaunchAgents" "$ROOT/data"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$VENV_PY</string><string>-m</string><string>church_bot</string><string>web</string><string>--no-browser</string>
  </array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PYTHONPATH</key><string>$ROOT/src</string>
    <key>PYTHONUTF8</key><string>1</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <!-- 程式當掉才自動重啟；正常結束（例如已經有另一個在跑）就不重啟 -->
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>StandardOutPath</key><string>$ROOT/data/autostart.log</string>
  <key>StandardErrorPath</key><string>$ROOT/data/autostart.log</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo "✅ 已設定開機自動執行。現在已經在背景跑了，管理網頁：http://127.0.0.1:8787"
echo "   取消請執行 autostart-off.sh"
