@echo off
call "%~dp0_env.bat"
if not exist "%VENV_PY%" (
  echo [X] 還沒安裝。請先雙擊 1-install.bat
  pause
  exit /b 1
)
title 服事提醒機器人：管理網頁（關掉這個視窗就關掉網頁）
"%VENV_PY%" -m church_bot web %*
if errorlevel 1 pause
