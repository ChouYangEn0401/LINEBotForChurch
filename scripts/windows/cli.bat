@echo off
rem For other programs (Telegram bot, Task Scheduler, scripts): no questions, no "press any key".
rem Runs one command and exits. Exit code: 0 = OK, 1 = something needs attention, 2 = settings problem.
rem   cli.bat send --scheduled   send only if it is the scheduled time and not sent yet (safe to call any time)
rem   cli.bat send               send now (Push; content already sent is skipped)
rem   cli.bat preview            show what would be sent (sends nothing)
rem   cli.bat check              health check + preview
setlocal
call "%~dp0_env.bat"
if not exist "%VENV_PY%" (
  echo [X] 還沒安裝。請先雙擊 1-install.bat
  exit /b 2
)
"%VENV_PY%" -m church_bot %*
exit /b %errorlevel%
