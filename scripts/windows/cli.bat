@echo off
rem For the Telegram bot (it does the scheduling) and other programs: no questions, no "press any key".
rem Runs one command and exits. Exit code: 0 = OK, 1 = something needs attention, 2 = settings problem.
rem   cli.bat send --retries 3 --retry-wait 300 --popup
rem                              weekly job: send (Push); content already sent is skipped; retry temporary
rem                              failures 3 times, 5 minutes apart; pop up a window if it still fails
rem   cli.bat send               send now, once
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
