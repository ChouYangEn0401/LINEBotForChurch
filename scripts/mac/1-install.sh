#!/usr/bin/env bash
# 安裝（第一次執行一次就好；之後更新程式也可以再執行一次）
# 用法：打開「終端機」，輸入 bash 加一個空格，把這個檔案拖進去，按 Enter。
source "$(dirname "$0")/_common.sh"

find_python() {
  for cmd in python3.11 python3.12 python3.13 python3 python; do
    if command -v "$cmd" >/dev/null 2>&1 &&
       "$cmd" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
      echo "$cmd"
      return 0
    fi
  done
  return 1
}

echo "=== 教會服事提醒機器人：安裝 ==="
if ! PY="$(find_python)"; then
  echo "❌ 找不到 Python 3.11 以上的版本。"
  echo "   請到 https://www.python.org/downloads/macos/ 下載安裝（或執行 brew install python@3.11），裝好再執行一次這個檔案。"
  exit 1
fi
echo "✅ 找到 $("$PY" --version)"

if [ -z "$VENV_PY" ]; then
  echo "… 建立獨立的 Python 環境（.venv）"
  "$PY" -m venv "$ROOT/.venv"
  VENV_PY="$(find_venv_python)"
fi

echo "… 安裝需要的套件（第一次大約 1～2 分鐘）"
"$VENV_PY" -m pip install --disable-pip-version-check -q --upgrade pip
"$VENV_PY" -m pip install --disable-pip-version-check -q -r "$ROOT/requirements.txt"

"$VENV_PY" -m church_bot init

echo ""
echo "🎉 安裝完成！"
echo "   下一步：執行 2-start.sh 打開管理網頁（方法一樣：bash 空格 把檔案拖進終端機 Enter）"
