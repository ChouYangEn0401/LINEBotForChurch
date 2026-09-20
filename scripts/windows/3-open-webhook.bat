@echo off
rem Opens a temporary public https address (Cloudflare Tunnel) so LINE can reach the Webhook.
rem Usage: double-click. Optional: 3-open-webhook.bat 9000  (if the web port is not 8787)
call "%~dp0_env.bat"
set "PORT=%~1"
if "%PORT%"=="" set "PORT=8787"
title Webhook 臨時網址（用完請關掉這個視窗）

where cloudflared >nul 2>nul
if errorlevel 1 (
  echo [X] 找不到 cloudflared，請先安裝（只要裝一次）：
  echo.
  echo     winget install --id Cloudflare.cloudflared
  echo.
  echo 裝好後，請關掉這個視窗，再重新雙擊 3-open-webhook.bat
  echo （如果剛裝好還是找不到，請重開機或登出再登入一次。）
  pause
  exit /b 1
)

powershell -NoProfile -Command "try { (New-Object Net.Sockets.TcpClient('127.0.0.1', %PORT%)).Close(); exit 0 } catch { exit 1 }"
if errorlevel 1 (
  echo [X] 管理網頁還沒開（連不到 127.0.0.1:%PORT%）。
  echo     請先雙擊 2-start.bat，等它開好之後再回來雙擊這個檔案。
  pause
  exit /b 1
)

echo 正在建立臨時網址，請稍等幾秒...
echo （用完請按 Ctrl+C 或直接關掉這個視窗；每週自動提醒不受影響）
echo.
cloudflared tunnel --url http://localhost:%PORT% 2>&1 | powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0_tunnel_filter.ps1"
echo.
echo 臨時網址已關閉。
pause
