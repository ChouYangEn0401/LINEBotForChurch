@echo off
rem Turn on weekly auto-send via Windows Task Scheduler (no window needs to stay open).
call "%~dp0_env.bat"
if not exist "%VENV_PY%" (
  echo [X] 還沒安裝。請先雙擊 1-install.bat
  pause
  exit /b 1
)
"%VENV_PY%" -m church_bot task on
echo.
pause
