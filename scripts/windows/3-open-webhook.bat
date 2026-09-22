@echo off
rem Free mode: LINE chat commands (/提醒, /我的ID, /我的名字) work while this window is open.
rem Starts the web server if it isn't running, opens a temporary public https address (Cloudflare Tunnel),
rem and registers it as the LINE Webhook URL automatically. Close this window when done.
rem Usage: double-click. Optional: 3-open-webhook.bat 9000  (if the web port is not 8787)
call "%~dp0_env.bat"
set "PORT=%~1"
if "%PORT%"=="" set "PORT=8787"
title 免費模式：LINE 指令可以用（用完關掉這個視窗）

if not exist "%VENV_PY%" (
  echo [X] 還沒安裝。請先雙擊 1-install.bat
  pause
  exit /b 1
)

set "CLOUDFLARED="
for /f "delims=" %%P in ('where cloudflared 2^>nul') do if not defined CLOUDFLARED set "CLOUDFLARED=%%P"
if not defined CLOUDFLARED if exist "%ProgramFiles(x86)%\cloudflared\cloudflared.exe" set "CLOUDFLARED=%ProgramFiles(x86)%\cloudflared\cloudflared.exe"
if not defined CLOUDFLARED if exist "%ProgramFiles%\cloudflared\cloudflared.exe" set "CLOUDFLARED=%ProgramFiles%\cloudflared\cloudflared.exe"
if not defined CLOUDFLARED (
  echo [X] 找不到 cloudflared，請先安裝（只要裝一次）：
  echo.
  echo     winget install --id Cloudflare.cloudflared
  echo.
  echo 裝好後，請關掉這個視窗，再重新雙擊 3-open-webhook.bat
  pause
  exit /b 1
)

call :webup
if not errorlevel 1 goto webready
echo 管理網頁還沒開，幫你在這個視窗裡開起來...
start "" /b "%VENV_PY%" -m church_bot web --no-browser
set /a TRIES=0
:waitweb
ping -n 2 127.0.0.1 >nul
call :webup
if not errorlevel 1 goto webready
set /a TRIES+=1
if %TRIES% geq 30 (
  echo [X] 管理網頁開不起來（連不到 127.0.0.1:%PORT%）。請把上面的訊息截圖給維護的人。
  pause
  exit /b 1
)
goto waitweb

:webready
echo 正在建立臨時網址，並自動登記到 LINE，請稍等十幾秒...
echo （用完請關掉這個視窗；每週自動提醒不受影響）
echo.
"%VENV_PY%" -m church_bot tunnel --port %PORT% --cloudflared "%CLOUDFLARED%"
echo.
echo 臨時網址已關閉，LINE 指令暫時不會有回應。
pause
exit /b 0

:webup
powershell -NoProfile -Command "try { (New-Object Net.Sockets.TcpClient('127.0.0.1', %PORT%)).Close(); exit 0 } catch { exit 1 }"
exit /b %errorlevel%
