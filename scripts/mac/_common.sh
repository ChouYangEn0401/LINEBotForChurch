#!/usr/bin/env bash
# 共用設定：被其他腳本載入（source），不要直接執行。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

# 程式碼在 src/ 裡；Windows 的 Git Bash 需要轉成 Windows 路徑（方便在 Windows 上測試這些 .sh）
SRC_DIR="$ROOT/src"
if command -v cygpath >/dev/null 2>&1; then SRC_DIR="$(cygpath -w "$SRC_DIR")"; fi
export PYTHONPATH="$SRC_DIR"
export PYTHONUTF8=1

find_venv_python() {
  if [ -x "$ROOT/.venv/bin/python" ]; then
    echo "$ROOT/.venv/bin/python"
  elif [ -x "$ROOT/.venv/Scripts/python.exe" ]; then
    echo "$ROOT/.venv/Scripts/python.exe"
  fi
}
VENV_PY="$(find_venv_python)"

require_venv() {
  if [ -z "$VENV_PY" ]; then
    echo "❌ 還沒安裝。請先執行 1-install.sh"
    exit 1
  fi
}
