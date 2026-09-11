@echo off
call "%~dp0_env.bat"
echo === 教會服事提醒機器人：安裝 ===
set "PY="
py -3.11 -c "import sys" >nul 2>nul && set "PY=py -3.11"
if not defined PY ( py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul && set "PY=py -3" )
if not defined PY ( python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul && set "PY=python" )
if not defined PY (
  echo [X] 找不到 Python 3.11 以上的版本。
  echo     請到 https://www.python.org/downloads/ 下載安裝。
  echo     安裝的第一個畫面，記得勾選「Add python.exe to PATH」，裝好後再雙擊一次這個檔案。
  start "" "https://www.python.org/downloads/windows/"
  pause
  exit /b 1
)
for /f "delims=" %%V in ('%PY% --version') do echo [OK] 找到 %%V
if not exist "%VENV_PY%" (
  echo ... 建立獨立的 Python 環境 .venv
  %PY% -m venv "%ROOT%\.venv" || goto :fail
)
echo ... 安裝需要的套件（第一次大約 1～2 分鐘）
"%VENV_PY%" -m pip install --disable-pip-version-check -q --upgrade pip || goto :fail
"%VENV_PY%" -m pip install --disable-pip-version-check -q -r "%ROOT%\requirements.txt" || goto :fail
"%VENV_PY%" -m church_bot init || goto :fail
echo.
echo 安裝完成！下一步：雙擊 2-start.bat 打開管理網頁。
pause
exit /b 0
:fail
echo.
echo [X] 安裝失敗。請把上面的訊息截圖，傳給維護的人。
pause
exit /b 1
