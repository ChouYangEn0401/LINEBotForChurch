@echo off
call "%~dp0_env.bat"
if not exist "%VENV_PY%" (
  echo [X] 還沒安裝。請先雙擊 1-install.bat
  pause
  exit /b 1
)
choice /c YN /m "確定要現在就發送到 LINE 群組嗎？（已經送過的不會重送）"
if errorlevel 2 exit /b 0
"%VENV_PY%" -m church_bot send %*
echo.
pause
